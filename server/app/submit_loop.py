import importlib
import random
import threading
import time
from collections import defaultdict
from typing import List, Any

from flask import current_app

import database
import reloader
from models import Flag, FlagStatus, SubmitResult


def get_fair_share(groups: List[List[Any]], limit: int):
    if not groups:
        return []

    groups = sorted(groups, key=len)
    places_left = limit
    group_count = len(groups)
    fair_share = places_left // group_count

    result = []
    residuals = []
    for group in groups:
        if len(group) <= fair_share:
            result += group

            places_left -= len(group)
            group_count -= 1
            if group_count > 0:
                fair_share = places_left // group_count
            # The fair share could have increased because the processed group
            # had a few elements. Sorting order guarantees that the smaller
            # groups will be processed first.
        else:
            selected = random.sample(group, fair_share + 1)
            result += selected[:-1]
            residuals.append(selected[-1])
    result += random.sample(residuals, min(limit - len(result), len(residuals)))

    random.shuffle(result)
    return result


def submit_flags(flags: List[Flag], config) -> List[SubmitResult]:
    module = importlib.import_module('protocols.' + config['SYSTEM_PROTOCOL'])

    try:
        return list(module.submit_flags(flags, config))
    except Exception as e:
        message = '{}: {}'.format(type(e).__name__, str(e))
        current_app.logger.error('Exception on submitting flags: %s', message)
        return [SubmitResult(item.flag, FlagStatus.QUEUED, message) for item in flags]


class SubmitThread(threading.Thread):
    def __init__(self, app):
        self.is_active = True
        self.app = app
        super(SubmitThread, self).__init__()

    def run(self):
        with self.app.app_context():
            self.app.logger.info('Starting submit loop')
            db = database.get(context_bound=False)

        while True:
            if not self.is_active:
                break

            submit_start_time = time.time()

            config = reloader.get_config()

            skip_time = round(submit_start_time - config['FLAG_LIFETIME'])
            db.execute("UPDATE flags SET status = ? WHERE status = ? AND time < ?",
                       (FlagStatus.SKIPPED.name, FlagStatus.QUEUED.name, skip_time))
            db.commit()

            cursor = db.execute("SELECT * FROM flags WHERE status = ?", (FlagStatus.QUEUED.name,))
            queued_flags = [Flag(**item) for item in cursor.fetchall()]

            if queued_flags:
                grouped_flags = defaultdict(list)
                for item in queued_flags:
                    grouped_flags[item.sploit, item.team].append(item)
                flags = get_fair_share(list(grouped_flags.values()), config['SUBMIT_FLAG_LIMIT'])

                with self.app.app_context():
                    self.app.logger.debug('Submitting %s flags (out of %s in queue)', len(flags), len(queued_flags))

                with self.app.app_context():
                    results = submit_flags(flags, config)

                rows = [(item.status.name, item.checksystem_response, item.flag) for item in results]
                db.executemany("UPDATE flags SET status = ?, checksystem_response = ? "
                               "WHERE flag = ?", rows)
                db.commit()

            submit_spent = time.time() - submit_start_time
            if config['SUBMIT_PERIOD'] > submit_spent:
                time.sleep(config['SUBMIT_PERIOD'] - submit_spent)

        with self.app.app_context():
            self.app.logger.info('Stopped submit loop')

    def join(self, timeout=None):
        self.is_active = False
        super(SubmitThread, self).join(timeout=timeout)
