
from __future__ import print_function, unicode_literals

import datetime

from django.core.management.base import BaseCommand
import pandas as pd

import scraper


class Command(BaseCommand):
    requires_system_checks = False
    help = "Check limits on registered GitHub API keys"

    def handle(self, *args, **options):
        api = scraper.GitHubAPI()
        now = datetime.datetime.now()

        df = pd.DataFrame(
            columns=("core_limit", "core_remaining",
                     "core_renews_in", "search_limit", "search_remaining",
                     "search_renews_in", "key"))
        for token in api.tokens:
            user = token.user
            values = {'key': token.token}
            token._check_limits()

            for api_class in token.limit:
                # geez, this code smells
                next_update = token.limit[api_class]['reset_time']
                if next_update is None:
                    renew = 'never'
                else:
                    tdiff = datetime.datetime.fromtimestamp(next_update) - now
                    renew = "%dm%ds" % divmod(tdiff.seconds, 60)
                values[api_class + '_renews_in'] = renew
                values[api_class + '_limit'] = token.limit[api_class]['limit']
                values[api_class + '_remaining'] = token.limit[api_class]['remaining']
            df.loc[user] = values

        print(df)
