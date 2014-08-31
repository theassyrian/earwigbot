# -*- coding: utf-8  -*-
#
# Copyright (C) 2009-2014 Ben Kurtovic <ben.kurtovic@gmail.com>
#
# Permission is hereby granted, free of charge, to any person obtaining a copy
# of this software and associated documentation files (the "Software"), to deal
# in the Software without restriction, including without limitation the rights
# to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
# copies of the Software, and to permit persons to whom the Software is
# furnished to do so, subject to the following conditions:
#
# The above copyright notice and this permission notice shall be included in
# all copies or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
# IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
# FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
# AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
# LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
# OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
# SOFTWARE.

from gzip import GzipFile
from math import log
from Queue import Empty, Queue
from socket import error
from StringIO import StringIO
from threading import Event, Lock, Thread
from time import time
from urllib2 import build_opener, URLError

from earwigbot import importer
from earwigbot.wiki.copyvios.markov import (
    EMPTY, EMPTY_INTERSECTION, MarkovChain, MarkovChainIntersection)
from earwigbot.wiki.copyvios.parsers import HTMLTextParser

tldextract = importer.new("tldextract")

__all__ = ["globalize", "localize", "CopyvioWorkspace"]

_is_globalized = False
_global_queues = None
_global_workers = []

def globalize(num_workers=8):
    """Cause all copyvio checks to be done by one global set of workers.

    This is useful when checks are being done through a web interface where
    large numbers of simulatenous requests could be problematic. The global
    workers are spawned when the function is called, run continuously, and
    intelligently handle multiple checks.

    This function is not thread-safe and should only be called when no checks
    are being done. It has no effect if it has already been called.
    """
    global _is_globalized, _global_queues
    if _is_globalized:
        return

    _global_queues = _CopyvioQueues()
    for i in xrange(num_workers):
        worker = _CopyvioWorker(_global_queues)
        worker.start("global-{0}".format(i))
        _global_workers.append(worker)
    _is_globalized = True

def localize():
    """Return to using page-specific workers for copyvio checks.

    This disables changes made by :func:`globalize`, including stoping the
    global worker threads.

    This function is not thread-safe and should only be called when no checks
    are being done.
    """
    global _is_globalized, _global_queues, _global_workers
    if not _is_globalized:
        return

    for i in xrange(len(_global_workers)):
        _global_queues.unassigned.put((StopIteration, None))
    _global_queues = None
    _global_workers = []
    _is_globalized = False


class _CopyvioSource(object):
    """Represents a single suspected violation source (a URL)."""

    def __init__(self, workspace, url, key, headers=None, timeout=5):
        self.url = url
        self.key = key
        self.headers = headers
        self.timeout = timeout
        self.confidence = 0.0
        self.chains = (EMPTY, EMPTY_INTERSECTION)

        self._workspace = workspace
        self._event = Event()

    def active(self):
        """Return whether or not this source needs to be filled out."""
        return not self._event.is_set()

    def complete(self, confidence, source_chain, delta_chain):
        """Complete the confidence information inside this source."""
        self.confidence = confidence
        self.chains = (source_chain, delta_chain)
        self._event.set()

    def cancel(self):
        """Deactivate this source without filling in the relevant data."""
        self._event.set()

    def join(self, until):
        """Block until this violation result is filled out."""
        if until:
            timeout = until - time()
            if timeout <= 0:
                return
            self._event.wait(timeout)


class _CopyvioQueues(object):
    """Stores data necessary to maintain the various queues during a check."""

    def __init__(self):
        self.lock = Lock()
        self.sites = {}
        self.unassigned = Queue()


class _CopyvioWorker(object):
    """A multithreaded URL opener/parser instance."""

    def __init__(self, queues, until=None):
        self._queues = queues
        self._until = until

        self._thread = None
        self._site = None
        self._queue = None
        self._opener = build_opener()

    def _open_url(self, source):
        """Open a URL and return its parsed content, or None.

        First, we will decompress the content if the headers contain "gzip" as
        its content encoding. Then, we will return the content stripped using
        an HTML parser if the headers indicate it is HTML, or return the
        content directly if it is plain text. If we don't understand the
        content type, we'll return None.

        If a URLError was raised while opening the URL or an IOError was raised
        while decompressing, None will be returned.
        """
        self._opener.addheaders = source.headers
        url = source.url.encode("utf8")
        try:
            response = self._opener.open(url, timeout=source.timeout)
        except (URLError, error):
            return None

        try:
            size = int(response.headers.get("Content-Length", 0))
        except ValueError:
            return None
        if size > 1024 ** 2:  # Ignore URLs larger than a megabyte
            return None

        ctype_full = response.headers.get("Content-Type", "text/plain")
        ctype = ctype_full.split(";", 1)[0]
        if ctype in ["text/html", "application/xhtml+xml"]:
            handler = lambda res: HTMLTextParser(res).strip()
        elif ctype == "text/plain":
            handler = lambda res: res.strip()
        else:
            return None

        try:
            content = response.read()
        except (URLError, error):
            return None

        if response.headers.get("Content-Encoding") == "gzip":
            stream = StringIO(content)
            gzipper = GzipFile(fileobj=stream)
            try:
                content = gzipper.read(2 * 1024 ** 2)
            except IOError:
                return None

        return handler(content)

    def _dequeue(self):
        """Remove a source from one of the queues."""
        if self._until:
            timeout = self._until - time()
            if timeout <= 0:
                return
        else:
            timeout = None

        with self._queues.lock:
            if self._queue:
                source = self._queue.get(timeout=timeout)
                if self._queue.empty():
                    del self._queues.sites[self._site]
                    self._queue = None
            else:
                site, queue = self._queues.unassigned.get(timeout=timeout)
                if site is StopIteration:
                    return StopIteration
                source = queue.get_nowait()
                if queue.empty():
                    del self._queues.sites[site]
                else:
                    self._site = site
                    self._queue = queue
            if not source.active():
                return self._dequeue()
            return source

    def _run(self):
        """Main entry point for the worker thread.

        We will keep fetching URLs from the queues and handling them until
        either we run out of time, or we get an exit signal that the queue is
        now empty.
        """
        while True:
            try:
                source = self._dequeue()
            except Empty:
                return
            if source is StopIteration:
                return
            text = self._open_url(source)
            if text:
                source.workspace.compare(source, MarkovChain(text))

    def start(self, name):
        """Start the worker in a new thread, with a given name."""
        self._thread = thread = Thread(target=self._run)
        thread.name = "cvworker-" + name
        thread.daemon = True
        thread.start()


class CopyvioWorkspace(object):
    """Manages a single copyvio check distributed across threads."""

    def __init__(self, article, min_confidence, until, logger, headers,
                 url_timeout=5, num_workers=8):
        self.best = _CopyvioSource(self, None, None)
        self.sources = []

        self._article = article
        self._logger = logger.getChild("copyvios")
        self._min_confidence = min_confidence
        self._until = until
        self._handled_urls = []
        self._is_finished = False
        self._compare_lock = Lock()
        self._source_args = {"workspace": self, "headers": headers,
                             "timeout": url_timeout}

        if _is_globalized:
            self._queues = _global_queues
            self._workers = _global_workers
        else:
            self._queues = _CopyvioQueues()
            for i in xrange(num_workers):
                worker = _CopyvioWorker(self._queues, until)
                worker.start("local-{0:04}.{1}".format(id(self) % 10000, i))
                self._workers.append(worker)

    def _calculate_confidence(self, delta):
        """Return the confidence of a violation as a float between 0 and 1."""
        def conf_with_article_and_delta(article, delta):
            """Calculate confidence using the article and delta chain sizes."""
            # This piecewise function, C_AΔ(Δ), was defined such that
            # confidence exhibits exponential growth until it reaches the
            # default "suspect" confidence threshold, at which point it
            # transitions to polynomial growth with lim (A/Δ)→1 C_AΔ(A,Δ) = 1.
            # A graph can be viewed here:
            # http://benkurtovic.com/static/article-delta_confidence_function.pdf
            ratio = delta / article
            if ratio <= 0.52763:
                return log(1 / (1 - ratio))
            else:
                return (-0.8939 * (ratio ** 2)) + (1.8948 * ratio) - 0.0009

        def conf_with_delta(delta):
            """Calculate confidence using just the delta chain size."""
            # This piecewise function, C_Δ(Δ), was derived from experimental
            # data using reference points at (0, 0), (100, 0.5), (250, 0.75),
            # (500, 0.9), and (1000, 0.95) with lim Δ→+∞ C_Δ(Δ) = 1.
            # A graph can be viewed here:
            # http://benkurtovic.com/static/delta_confidence_function.pdf
            if delta <= 100:
                return delta / (delta + 100)
            elif delta <= 250:
                return (delta - 25) / (delta + 50)
            elif delta <= 500:
                return (10.5 * delta - 750) / (10 * delta)
            else:
                return (delta - 50) / delta

        d_size = float(delta.size)
        return max(conf_with_article_and_delta(self._article.size, d_size),
                   conf_with_delta(d_size))

    def _finish_early(self):
        """Finish handling links prematurely (if we've hit min_confidence)."""
        if self._is_finished:
            return
        self._logger.debug("Confidence threshold met; cancelling remaining sources")
        with self._queues.lock:
            for source in self.sources:
                source.cancel()
            self._is_finished = True

    def enqueue(self, urls, exclude_check=None):
        """Put a list of URLs into the various worker queues.

        *exclude_check* is an optional exclusion function that takes a URL and
        returns ``True`` if we should skip it and ``False`` otherwise.
        """
        for url in urls:
            if self._is_finished:
                break
            if url in self._handled_urls:
                continue
            self._handled_urls.append(url)
            if exclude_check and exclude_check(url):
                continue

            try:
                key = tldextract.extract(url).registered_domain
            except ImportError:  # Fall back on very naive method
                from urlparse import urlparse
                key = u".".join(urlparse(url).netloc.split(".")[-2:])

            source = _CopyvioSource(url=url, key=key, **self._source_args)
            logmsg = u"enqueue(): {0} {1} -> {2}"
            with self._queues.lock:
                if key in self._queues.sites:
                    self._logger.debug(logmsg.format("append", key, url))
                    self._queues.sites[key].put(source)
                else:
                    self._logger.debug(logmsg.format("new", key, url))
                    self._queues.sites[key] = queue = Queue()
                    queue.put(source)
                    self._queues.unassigned.put((key, queue))

    def wait(self):
        """Wait for the workers to finish handling the sources."""
        self._logger.debug("Waiting on {0} sources".format(len(self.sources)))
        for source in self.sources:
            source.join(self._until)

    def compare(self, source, source_chain):
        """Compare a source to the article, and update the best known one."""
        delta = MarkovChainIntersection(self._article, source_chain)
        conf = self._calculate_confidence(delta)
        source.complete(conf, source_chain, delta)
        self._logger.debug(u"compare(): {0} -> {1}".format(source.url, conf))

        with self._compare_lock:
            if conf > self.best.confidence:
                self.best = source
                if conf >= self._min_confidence:
                    self._finish_early()
