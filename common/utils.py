
from __future__ import unicode_literals

import networkx as nx
import pandas as pd

from collections import defaultdict
import logging
import re

from common import decorators as d
from common import mapreduce
from common import versions
import scraper

# ecosystems
import npm
import pypi

ECOSYSTEMS = {
    'npm': npm,
    'pypi': pypi
}

logger = logging.getLogger("ghd")
fs_cache = d.fs_cache('common')

# default start dates for ecosystem datasets. It is used for sanity checks
START_DATES = {
    'npm': '2010',
    'pypi': '2005'
}

""" This lookup is used by parse_license()
Since many license strings contain several (often conflicting) licenses,
the least restrictive license takes precedence.

https://en.wikipedia.org/wiki/Comparison_of_free_and_open-source_software_licenses
"""

LICENSE_TYPES = (
    (  # permissive
        ('apache', 'Apache'),
        ('isc', 'ISC'),
        ('mit', 'MIT'),
        ('bsd', 'BSD'),
        ('wtf', 'WTFPL'),
        ('public', 'PD'),
        ('unlicense', 'PD'),
    ),
    (  # somewhat restrictive
        ('mozilla', 'MPL'),
        # 'mpl' will also match words like 'simple' and 'example'
    ),
    (  # somewhat permissive
        ('lesser', 'LGPL'),
        ('lgpl', 'LGPL'),
    ),
    (  # strong copyleft
        ('general public', 'GPL'),
        ('gpl', 'GPL'),
        ('affero', 'GPL'),
        ('CC-BY-SA', 'CC-BY-SA'),
    ),
    (  # permissive again
        ('CC-BY', 'CC'),
        ('creative', 'CC'),
    ),
)


def get_ecosystem(ecosystem):
    """ Return ecosystem obj if supported, raise ValueError otherwiese """
    if ecosystem not in ECOSYSTEMS:
        raise ValueError(
            "Ecosystem %s is not supported. Only (%s) are supported so far" % (
                ecosystem, ",".join(ECOSYSTEMS.keys())))
    return ECOSYSTEMS[ecosystem]


@fs_cache
def package_urls(ecosystem):
    # type: (str) -> pd.Series
    """ A shortcut to get list of packages having identified repo URL
    Though it looks trivial, it is a rather important method.
    >>> urls = package_urls("pypi")
    >>> isinstance(urls, pd.Series)
    True
    >>> len(urls) > 50000
    True
    """
    es = get_ecosystem(ecosystem)
    urls = es.packages_info()["url"].dropna()

    def supported(url):
        try:
            scraper.get_provider(url)
        except NotImplementedError:
            return False
        return True

    urls = urls[urls.map(supported)]

    # this part normalizes URLs, e.g. by removing trailing .git from GitHub URLs
    def normalize(url):
        provider, project_url = scraper.get_provider(url)
        return provider.canonical_url(project_url)

    urls = urls.map(normalize)

    # this is necessary to get rid of false URLS, such as:
    # - meta-urls, e.g.
    #       http://github.com/npm/deprecate-holder.git
    #       http://github.com/npm/security-holder.git
    # - foundries, i.e. repositories hosting swarms of packages at once
    #       github.com/micropython/micropython-lib
    #       https://bitbucket.org/ronaldoussoren/pyobjc/src
    # - false records generated by code generators
    #       "This project was generated with angular-cli"
    #       "This project was bootstrapped with [Create React App]"
    #       github.com/swagger-api/swagger-codegen
    # NPM: 446K -> 389K
    # PyPI: 91728 -> 86892
    urls = urls[urls.map(urls.value_counts()) == 1]

    def exists(project_name, url):
        logger.info(project_name)
        provider, project_url = scraper.get_provider(url)
        return provider.project_exists(project_url)

    # - more than 16 threads make GitHub to choke even on public urls
    # - some malformed URLs will result in NaN (e.g. NPM abwa-gulp and
    #       barco-jobs), so need to fillna()
    se = mapreduce.map(urls, exists, num_workers=16).fillna(False)

    return urls[se]


def get_repo_usernames(urls):
    # type: (pd.Series) -> pd.DataFrame
    """
    This function is used by user_info to extract name of repository owner
    from its URL. It works so far, but violates abstraction and should be
    refactored at some point
    :param: pd.Series with urls, e.g. github.com/pandas-dev/pandas
    :return pd.Dataframe with columns
        index: url index, package name if package_urls() is used as inputs
        - provider_name: str, {github.com|bitbucket.org|gitlab.com}
        - login: str, provider-specific login

    >>> urls = s = pd.Series(
        ["github.com/pandas-dev/pandas",
        "github.com/user2589/ghd",
        "github.com/dkhsd/asdf"])
    >>> usernames = get_repo_usernames(urls)
    >>> isinstance(usernames, pd.DataFrame)
    True
    >>> len(urls) == len(usernames)
    True
    >>> all(col in usernames.columns for col in ('provider_name', 'login'))
    True
    >>> usernames.loc["pandas", "provider_name"]
    'github.com'
    >>> usernames.loc['pandas', 'login']
    'pandas-dev'
    """
    def gen():
        for _, url in urls.items():
            provider_name, project_url = scraper.parse_url(url)
            # Assuming urls come from package_urls,
            # we already know the provider is supported
            yield {
                'provider_name': provider_name,
                'login': project_url.split("/", 1)[0]
            }
    return pd.DataFrame(gen(), index=urls.index)


@d.fs_cache('common', 2)
def user_info(ecosystem):
    """ Return user profile fields
    Originally this method was created to differentiate org from user accounts

    :param ecosystem: {npm|pypi}
    :return: pd.DataFrame with columns:
        - provider_name: {github.com|bitbucket.org|gitlab.com}
        - login: username on the provider website
        - created_at: str, ISO timestamp
        - org: bool, whether it is an organization account (vs personal)
        - public_repos: int
        - followers: int
        - following: int
    """

    def get_user_info(_, row):
        # single column dataframe is used instead of series to simplify
        # result type conversion
        username = row["login"]
        logger.info("Processing %s", username)
        fields = ['created_at', 'login', 'type', 'public_repos',
                  'followers', 'following']
        provider_name, _ = scraper.parse_url(row["url"])
        provider, _ = scraper.get_provider(row["url"])
        try:
            data = provider.user_info(username)
        except scraper.RepoDoesNotExist:
            return {}
        res = {field: data.get(field) for field in fields}
        res["provider_name"] = provider_name
        return res

    # Since we're going to get many fields out of one, to simplify type
    # conversion it makes sense to convert to pd.DataFrame.
    # by the same reason, user_info() above gets row and not url value
    urls = package_urls(ecosystem)
    urls.index = urls  # will need it in get_user_info

    # it's going to be a pd.DataFrame(provider_name, login, url)
    usernames = get_repo_usernames(urls).reset_index()
    usernames = usernames.groupby(["provider_name", "login"]).first()
    # GitHub seems to ban IP (will get HTTP 403) if use 8 workers
    ui = mapreduce.map(usernames.reset_index(), get_user_info, num_workers=6)
    # TODO: move to provider
    ui["org"] = ui["type"].map({"Organization": True, "User": False})
    return ui.drop(["type"], axis=1).set_index("login", drop=True)


def parse_license(license):
    """ Map raw license string to either a feature, either a class or a numeric
    measure, like openness.
    ~1 second for NPM, no need to cache
    - 3295 unique values in PyPI (lowercase for normalization)
    + gpl + general public - lgpl - lesser = 575 + 152 - 152 + 45 = 530
        includes affero
    + bsd: 358
    + mit: 320
    + lgpl + lesser = 152 + 45 = 197
    + apache: 166
    + creative: 44
    + domain: 34
    + mpl - simpl - mple = 29
    + zpl: 26
    + wtf: 22
    + zlib: 7
    - isc: just a few, but MANY in NPM
    - copyright: 763
        "copyright" is often (50/50) used with "mit"
    """
    if license and pd.notnull(license):
        license = license.lower()
        # the most permissive ones come first
        for license_types in LICENSE_TYPES:
            for token, license_type in license_types:
                if token in license:
                    return license_type
    return None


def count_values(df):
    # type: (pd.DataFrame) -> pd.DataFrame
    """ Count number of values in lists/sets
    It is initially introduced to count dependencies
    >>> c = count_values(pd.DataFrame({1:[set(), set(range(4)), [1,2,3,2,4]]}))
    >>> c.loc[0, 1]
    0
    >>> c.loc[1, 1]
    4
    >>> c.loc[2, 1]
    5
    """
    # takes around 20s for full pypi history

    def count(s):
        return len(s) if s and pd.notnull(s) else 0

    if isinstance(df, pd.DataFrame):
        return df.applymap(count)
    elif isinstance(df, pd.Series):
        return df.apply(count)


@d.memoize
def upstreams(ecosystem):
    # type: (str) -> pd.DataFrame
    """ Get a dataframe with upstream dependencies sliced per month
     ~66s for pypi, doesn't make sense to cache in filesystem

    :param ecosystem: str, {npm|pypi}
    :return pd.DataFrame, df.loc[package, month] = set([upstreams])
    """
    def gen():
        es = get_ecosystem(ecosystem)
        deps = es.dependencies().sort_values("date")
        # will drop 101 record out of 4M for npm
        deps = deps[pd.notnull(deps["date"])]
        # otherwise, there is a package in NPM dated 1970 which increases
        # dataframe size manyfold
        deps = deps[deps["date"] > START_DATES[ecosystem]]
        deps['deps'] = deps['deps'].map(
            lambda x: set(x.split(",")) if x and pd.notnull(x) else set())
        # remove alpha releases, 835K-> 744K (PyPI)
        deps = deps[~(deps["version"].map(versions.is_alpha))]

        # for several releases per month, use the last value
        df = deps.groupby([deps.index, deps['date'].str[:7].rename('month')]
                          ).last().reset_index().sort_values(["name", "month"])

        last_release = ""
        last_package = ""
        for _, row in df.iterrows():
            if row["name"] != last_package:
                last_release = ""
                last_package = row["name"]
            # remove backports
            if versions.compare(row["version"], last_release) < 0:
                continue
            last_release = row["version"]
            yield row

    df = pd.DataFrame(gen(), columns=["name", "month", "deps"])

    # pypi was started around 2000, first meaningful numbers around 2005
    # npm was started Jan 2010, first meaningful release 2010-11
    # no need to cut off anything
    idx = [dt.strftime("%Y-%m")
           for dt in pd.date_range(df['month'].min(), 'now', freq="M")]

    deps = df.set_index(["name", "month"], drop=True)["deps"]
    # ffill can be dan with axis=1; Transpose here is to reindex
    return deps.unstack(level=0).reindex(idx).fillna(method='ffill').T


@d.memoize
def downstreams(ecosystem):
    # type: (str) -> pd.DataFrame
    """ Basically, reversed upstreams
    +25s to upstreams execution on PyPI dataset

    :param ecosystem: str, {pypi|npm}
    :return: pd.DataFrame, df.loc[project, month] = set([*projects])
    """
    uss = upstreams(ecosystem)

    def gen(row):
        s = defaultdict(set)
        for pkg, dss in row.items():
            if dss and pd.notnull(dss):
                # add package as downstream to each of upstreams
                for ds in dss:
                    s[ds].add(pkg)
        return pd.Series(s, name=row.name, index=row.index)

    return uss.apply(gen, axis=0)


def backporting(ecosystem, window=12):
    """
    In many cases "backporting" is caused by labeling errors so don't expect
    16s for PyPI

    How to test (face validity):
    pandas doesn't do backporting
    numpy did once (1.7.2, 2013-12-31)
        once they mislabeled a release (1.10.3 after 1.10.4, 2016-04-20)
    django does backport all the time (they always support at least couple
        most recent versions)

    :param ecosystem: str {npm|pypi}
    :param window: number of month to consider exercising backporting since
        observed
    :return: pd.Dataframe, df.loc[package, month] = <bool>
    """
    es = get_ecosystem(ecosystem)
    deps = es.dependencies().reset_index().sort_values(["name", "date"])
    deps = deps[~(deps["version"].map(versions.is_alpha))]
    deps["prev_version"] = deps["version"].shift(1)
    deps["prev_name"] = deps["name"].shift(1)
    deps = deps[deps["name"] == deps["prev_name"]]
    deps = deps[["name", "version", "prev_version", "date"]]
    deps["cmp"] = deps.apply(
            lambda row: versions.compare(row["version"], row["prev_version"]),
            axis=1)
    backported = deps.loc[deps["cmp"] < 0, ["name", "date"]]
    backported["date"] = backported["date"].str[:7]
    backported["backported"] = 1

    idx = [dt.strftime("%Y-%m")
           for dt in pd.date_range(backported['date'].min(), 'now', freq="M")]

    backported = backported.set_index(["name", "date"], drop=True)
    backported = backported.groupby(["name", "date"]).first()
    df = backported.unstack(level=0).reindex(idx).fillna(0)
    # level_0 is an artifact of multiindex
    df = df.T.reset_index().set_index("name", drop=True).drop("level_0", axis=1)
    df = df.rolling(window=window, min_periods=1, axis=1).mean()
    return df.reindex(deps["name"].unique(), fill_value=0).astype(bool)


def cumulative_dependencies(deps):
    """
   ~160 seconds for pypi upstreams, ?? for downstreams
   Tests:
         A      B
       /  \
      C    D
    /  \
   E    F
   >>> down = pd.DataFrame({
        1: [set(['c', 'd']), set(), set(['e', 'f']), set(), set(), set()]},
            index=['a', 'b', 'c', 'd', 'e', 'f'])
   >>> len(cumulative_dependencies(down).loc['a', 1])
   5
   >>> len(cumulative_dependencies(down).loc['c', 1])
   2
   >>> len(cumulative_dependencies(down).loc['b', 1])
   0
   """
    def gen(dependencies):
        cumulative_upstreams = {}

        def traverse(pkg):
            if pkg not in cumulative_upstreams:
                cumulative_upstreams[pkg] = set()  # prevent infinite loop
                ds = dependencies[pkg]
                if ds and pd.notnull(ds):
                    cumulative_upstreams[pkg] = set.union(
                        ds, *(traverse(d) for d in ds if d in dependencies))
            return cumulative_upstreams[pkg]

        return pd.Series(dependencies.index, index=dependencies.index).map(
            traverse).rename(dependencies.name)

    return deps.apply(gen, axis=0)


def centrality(how, graph):
    # type: (str, nx.Graph) -> dict
    """ A wrapper for networkx centrality methods to allow for parametrization

    :param how: str, networkx centrality method
    :param graph: nx.Graph or nx.DiGraph
    :return: dict, {node_label: centrality_value}
    """
    if not how.endswith("_centrality") and how not in \
            ('communicability', 'communicability_exp', 'estrada_index',
             'communicability_centrality_exp', "subgraph_centrality_exp",
             'dispersion', 'betweenness_centrality_subset', 'edge_load'):
        how += "_centrality"
    assert hasattr(nx, how), "Unknown centrality measure: " + how
    return getattr(nx, how)(graph)


@fs_cache
def dependencies_centrality(ecosystem, centrality_type):
    """
    [edge_]current_flow_closeness is not defined for digraphs
    current_flow_betweenness - didn't try
    communicability*
    estrada_index
    """
    logger.info("Collecting dependencies data..")
    uss = upstreams(ecosystem)

    def gen(stub):
        # stub = uss column
        logger.info("Processing %s", stub.name)
        g = nx.DiGraph()
        for pkg, us in stub.items():
            if not us or pd.isnull(us):
                continue
            for u in us:  # u is upstream name
                g.add_edge(pkg, u)

        return pd.Series(centrality(centrality_type, g), index=stub.index)

    return uss.apply(gen, axis=0).fillna(0)


@d.memoize
def contributors(ecosystem, months=1):
    # type: (str) -> pd.DataFrame
    """ Get a historical list of developers contributing to ecosystem projects
    ~7s when cached (PyPI), few minutes otherwise

    :param ecosystem: {"pypi"|"npm"}
    :param months int(=1), use contributors for this number of last months
    :return: pd.DataFrame, index is projects, columns are months, cells are
        sets of str github usernames
    >>> c = contributors("pypi")
    >>> isinstance(c, pd.DataFrame)
    True
    >>> 50000 < len(c) < 200000
    True
    >>> 150 < len(c.columns) < 200
    True
    >>> len(c.loc["django", "2017-12"]) > 30  # 32, as of Jan 2018
    True
    """
    assert months > 0

    @fs_cache
    def _contributors(*_):
        start = START_DATES[ecosystem]
        columns = [dt.strftime("%Y-%m")
                   for dt in pd.date_range(start, 'now', freq="M")]

        def gen():
            log = logging.getLogger("ghd.common._contributors")

            for package, repo in package_urls(ecosystem).items():
                log.info(package)
                try:
                    s = scraper.commit_user_stats(repo).reset_index()[
                        ['authored_date', 'author']].groupby('authored_date').agg(
                        lambda df: set(df['author']))['author'].rename(
                        package).reindex(columns)
                except scraper.RepoDoesNotExist:
                    continue
                if months > 1:
                    s = pd.Series(
                        (set().union(*[c for c in s[max(0, i-months+1):i+1]
                                     if c and pd.notnull(c)])
                         for i in range(len(columns))),
                        index=columns, name=package)
                yield s

        return pd.DataFrame(gen(), columns=columns).applymap(
            lambda s: ",".join(str(u) for u in s) if s and pd.notnull(s) else "")

    return _contributors(ecosystem, months).applymap(
        lambda s: set(s.split(",")) if s and pd.notnull(s) else set())


# @fs_cache
def contributors_centrality(ecosystem, centrality_type):
    """ Get centrality measures for contributors graph.
    Doesn't make much sense for centrality_types other than degree
    12s

    >>> cc = contributors_centrality("pypi", "degree")
    >>> isinstance(cc, pd.DataFrame)
    True
    >>> 50000 < len(cc) < 200000
    True
    >>> 150 < len(cc.columns) < 200
    True
    >>> cc.loc["django", "2017-12"] > 40  # ~90 for the late 2017
    """
    log = logging.getLogger("ghd.common.contributors_centrality")

    log.info("Getting contributors..")
    contras = contributors(ecosystem)
    # {in|out}_degree is not defined for undirected graphs

    log.info("Processing contributors centrality by month..")

    def gen(stub):
        # stub is a Series corresponding to a month
        log.info(stub.name)
        projects = defaultdict(set)  # projects[contributor] = set(projects)

        # first, find what projects every contributor contributed to
        for project, contributors_set in stub.items():
            if not contributors_set or pd.isnull(contributors_set):
                continue
            for contributor in contributors_set:
                projects[contributor].add(project)

        projects["-"] = set()
        g = nx.Graph()

        # then, for all pairs add an edge to the graph
        for project, contributors_set in stub.items():
            for contributor in contributors_set:
                for p in projects[contributor]:
                    if p > project:  # avoid duplicating edges
                        g.add_edge(project, p)

        ct = centrality(centrality_type, g)
        # ct is now nx.DegreeView, need to transform into dict
        return pd.Series(dict(ct), index=stub.index)

    return contras.apply(gen, axis=0).fillna(0)


def survival_data(ecosystem, smoothing=1):
    """
    :param ecosystem: ("npm"|"pypi")
    :param smoothing:  number of month to average over
    :return: pd.Dataframe with columns:
         age, date, project, dead, last_observation
         commercial, university, org, license,
         commits, contributors, q50, q70, q90, gini,
         issues, non_dev_issues, submitters, non_dev_submitters
         downstreams, upstreams, transitive downstreams, transitive upstreams,
         contributors centrality,
         dependencies centrality
    """
    log = logging.getLogger("ghd.survival")
    death_window = 12
    death_threshold = 1.0

    def gen():
        es = get_ecosystem(ecosystem)
        urls = package_urls(ecosystem)[:20]
        log.info("Getting package info and user profiles..")
        usernames = get_repo_usernames(urls)
        ui = user_info(ecosystem)
        pkginfo = es.packages_info()

        # FIXME: uncomment when done with testing
        # log.info("Dependencies counts..")
        # uss = upstreams(ecosystem)  # upstreams, every cell is a set()
        # dss = downstreams(uss)  # downstreams, every cell is a set()
        # usc = count_values(uss)  # upstream counts
        # dsc = count_values(dss)  # downstream counts
        # # transitive counts
        # t_usc = count_values(cumulative_dependencies(uss))
        # t_dsc = count_values(cumulative_dependencies(dss))

        log.info("Dependencies centrality..")
        dc_closeness = dependencies_centrality("pypi", "closeness")
        dc_degree = dependencies_centrality("pypi", "in_degree")
        dc_closeness = dependencies_centrality("pypi", "closeness")

        log.info("Collecting dataset..")

        for project_name, url in urls.items():
            log.info(project_name)

            try:
                cs = scraper.commit_stats(url)
            except scraper.RepoDoesNotExist:
                # even though package_url checks for repos existance, it could
                # be deleted later
                continue

            if not len(cs):  # repo does not exist
                continue
            uname = usernames.loc[project_name]
            df = pd.DataFrame({
                'age': range(len(cs)),
                'project': project_name,
                'dead': 1,
                'last_observation': 0,
                'org': ui.loc[uname["provider_name"], uname["login"]]["org"][0],
                'license': parse_license(pkginfo.loc[project_name, "license"]),
                'commercial': scraper.commercial_involvement(url).reindex(
                    cs.index, fill_value=0),
                'university': scraper.university_involvement(url).reindex(
                    cs.index, fill_value=0),
                'commits': cs,
                'contributors': scraper.commit_users(url).reindex(
                    cs.index, fill_value=0),
                # 'q50': scraper.contributions_quantile(url, 0.5).reindex(
                #     cs.index, fill_value=0),
                # 'q70': scraper.contributions_quantile(url, 0.7).reindex(
                #     cs.index, fill_value=0),
                'q90': scraper.contributions_quantile(url, 0.9).reindex(
                    cs.index, fill_value=0),
                # 'gini': scraper.commit_gini(url).reindex(cs.index),
                'issues': scraper.new_issues(url).reindex(
                    cs.index, fill_value=0),
                'non_dev_issues': scraper.non_dev_issue_stats(url).reindex(
                    cs.index, fill_value=0),
                'submitters': scraper.submitters(url).reindex(
                    cs.index, fill_value=0),
                'non_dev_submitters': scraper.non_dev_submitters(url).reindex(
                    cs.index, fill_value=0),
                # 'upstreams': usc.loc[project_name, cs.index],
                # 'downstreams': dsc.loc[project_name, cs.index],
                # 't_downstreams': t_dsc.loc[project_name, cs.index],
                # 't_upstreams': t_usc.loc[project_name, cs.index],
            })

            dead = (cs[::-1].rolling(window=death_window).mean(
                )[:death_window - 2:-1] < death_threshold).shift(-1).fillna(method='ffill')
            df['dead'] = dead
            death = dead[dead].index.min()
            if death and pd.notnull(death):
                df = df.loc[:death]

            # FIXME: centrality
            # 'cc_X': None,
            # 'dc_X': None

            df = df.rolling(window=smoothing, min_periods=1).mean()
            df.iloc[-1, df.columns.get_loc("last_observation")] = 1
            df = df[((df["age"] % smoothing) == 0) | df["last_observation"]]

            for _, row in df.iterrows():
                yield row

    return pd.DataFrame(gen()).reset_index(drop=True)
