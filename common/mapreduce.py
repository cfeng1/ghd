
import pandas as pd

import collections

from common import threadpool


class MapReduce(object):
    """ Helper to process large volumes of information
    It employes configured backend

    Workflow:
        (input of every function passed to the next one)
        preprocess -> map -> reduce -> postprocess

        at least map() or reduce() should be defined.
        pre/post processing is intended for reusable classes, useless otherwise

    Use:
        class Processor(MapRedue):
            # NOTE: all methods are static, i.e. no self

            def preprocess(*data):
                # gets raw input data
                return single_object

            def map(key, value)
                # depending on input, key, value defined as a result of:
                # .iterrows(), .items(), or enumerate, whatever found first
                processed_value = process(value)
                return key, processed_value



    """
    # change these to override default backend
    backend_config = None  # keywords to init backend object (Threadpool)

    # methods
    preprocess = None
    map = None
    reduce = None
    postprocess = None
    @staticmethod
    def __new__(cls, data):
        """ An intro to Python object creation:
        1. Python checks for metaclass
            - object.__metaclass__
            - parent.__metaclass__
            - module.__metaclass__
            - type by default
            Metaclass is a callable with args:
                - class_name
                - tuple of parent classes
                - dict of attributes and their values
            Metaclass returns a CLASS (not an object)
        2. Python calls the class.__new__()  # note the call is static
            NOTE: it is only called for new-style classes, but you don't
                have to worry about this until you time travelled back to 2013
            __new__ accepts class, args and kwargs,
                and returns an object instance, calling __init__ along the way
            This makes possible to use __new__ as a static __call__, which is
            exploited in this article: https://habrahabr.ru/post/145835/
            And so will we.

        Wokflow in this method:
            - accept list of inputs as the only argument
            - use schedule(map) to transofrm the input
            - reduce will be used as a success callback to form result
        """

        assert cls.map or cls.reduce, "MapReduce subclasses are expected to " \
                                      "have at least one of map() or reduce()" \
                                      " methods defined."

        if cls.preprocess:
            data = cls.preprocess(data)

        assert isinstance(data, collections.Iterable), "Iterable expected"

        if cls.map:
            backend = threadpool.ThreadPool(**(cls.backend_config or {}))
            iterable = None
            for method in ('iterrows', 'items'):
                if hasattr(data, method):
                    iterable = getattr(data, method)()
                    break
            if iterable is None:
                iterable = enumerate(data)

            mapped = {}

            def collect(res):
                key, value = res
                mapped[key] = value

            for key, value in iterable:
                backend.submit(cls.map, key, value, callback=collect)
            backend.shutdown()

            if isinstance(data, pd.DataFrame):
                data = pd.DataFrame.from_dict(mapped, orient='index').reindex(data.index)
            elif isinstance(data, pd.Series):
                data = pd.Series(mapped).reindex(data.index)
            elif isinstance(data, (list, tuple)):
                data = type(data)(mapped[i] for i in range(len(mapped)))
            else:
                data = mapped

        if cls.reduce:
            data = cls.reduce(data)

        if cls.postprocess:
            data = cls.postprocess(data)

        return data
