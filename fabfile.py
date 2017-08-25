
from __future__ import print_function

import os

from fabric import api as fab


def test():
    fab.local("python -m unittest pypi.test")
    fab.local("python -m unittest scraper.test")


def clean():
    fab.local("find -type d -name __pycache__ -exec rm -rf {} +")


def install():
    # see requirements.txt for details
    reqs = (
        "libarchive-dev",
        "docker-compose",
        "yajl-tools"
    )
    for req in reqs:
        if os.system("dpkg -l %s > /dev/null 2> /dev/null" % req) > 0:
            fab.sudo("apt-get -y install " + req)
    fab.local("pip install --user -r requirements.txt")