"""Session-scoped, bounded caches; no open PDFs, image buffers or worker pools."""
from collections import OrderedDict
from copy import deepcopy
from dataclasses import replace
from pathlib import Path
import threading

from magiczip import engine


class SourceCache:
    def __init__(self, capacity=512):
        self.capacity = capacity
        self.lock = threading.RLock()
        self.stripes = [threading.Lock() for _ in range(16)]
        self.slots = threading.BoundedSemaphore(2)
        self.inspections, self.validations, self.prepared = OrderedDict(), OrderedDict(), OrderedDict()
        self.hits = self.misses = 0

    def clear(self):
        with self.lock:
            self.inspections.clear()
            self.validations.clear()
            self.prepared.clear()
            self.hits = self.misses = 0

    def _get(self, cache, key):
        with self.lock:
            value = cache.get(key)
            if value is not None:
                cache.move_to_end(key)
                return deepcopy(value)

    def _put(self, cache, key, value, capacity):
        with self.lock:
            cache[key] = deepcopy(value)
            cache.move_to_end(key)
            while len(cache) > capacity:
                cache.popitem(last=False)

    def inspect(self, source, check=lambda: None):
        if source.kind == 'directory':
            return source
        check()
        path = Path(source.path)
        signature = engine.file_signature(path)
        if source.stat_signature and signature != source.stat_signature:
            raise ValueError(f'{source.name} cambió desde el inventario. Vuelve a agregarlo.')
        key = str(path.resolve()).casefold(), signature
        stripe = self.stripes[hash(key) % len(self.stripes)]
        while not stripe.acquire(timeout=.05):
            check()
        try:
            check()
            cached = self._get(self.inspections, key)
            if cached is None:
                if not source.pending_inspection and source.sha256 and not source.error:
                    cached = source
                else:
                    while not self.slots.acquire(timeout=.05):
                        check()
                    try:
                        check()
                        cached = engine.inspect_source(path, deep=False, check=check)
                    finally:
                        self.slots.release()
                check()
                if cached.error:
                    return replace(cached, id=source.id, relative=source.relative,
                                   collection=source.collection, zip_relative=source.zip_relative)
                self._put(self.inspections, key, cached, self.capacity)
                with self.lock:
                    self.misses += 1
            else:
                with self.lock:
                    self.hits += 1
            check()
            # Never mutate a Source shared with the inventory or another request.
            return replace(cached, id=source.id, relative=source.relative, collection=source.collection,
                           zip_relative=source.zip_relative, pending_inspection=False)
        finally:
            stripe.release()

    def validation(self, sha256):
        return self._get(self.validations, sha256)

    def remember_validation(self, sha256, verification):
        self._put(self.validations, sha256, verification, self.capacity)

    def optimized(self, key):
        record = self._get(self.prepared, key)
        if record and all(Path(part['path']).is_file() for part in record['parts']):
            return record

    def remember_optimized(self, key, record):
        self._put(self.prepared, key, record, 16)
