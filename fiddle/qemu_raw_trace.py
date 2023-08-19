#!/usr/bin/env python
#
# Pretty-printer for simple trace backend binary trace files
#
# Copyright IBM, Corp. 2010
# Modified by bx (bx@cs.dartmouth.edu) 2017
# This work is licensed under the terms of the GNU GPL, version 2.  t

import struct
import inspect
import sys
import os
from config import Main
qemu = Main.object_config_lookup("Software", "qemu")
sys.path.append(os.path.join(qemu.root, "scripts"))
try:
    from tracetool import _read_events
    from tracetool import Event
    from tracetool.backend.simple import is_string
except ImportError as e:
    sys.stderr.write("QEMU's tracetool is required to analyze QEMU watchpoint events but was not found in your python path.\n")
    sys.stderr.write("Please download tracetool.py from %s and copy it to one of the following directories in your path:" % "https://raw.githubusercontent.com/qemu/qemu/86b5aacfb972ffe0fa5fac6028e9f0bc61050dda/scripts/tracetool.py\n")
    sys.stderr.write("%s\n" % sys.path)
    raise e
import db_info

header_event_id = 0xffffffffffffffff
header_magic = 0xf2b177cb0aa429b4
dropped_event_id = 0xfffffffffffffffe

log_header_fmt = '=QQQ'
rec_header_fmt = '=QQII'


def read_header(fobj, hfmt):
    '''Read a trace record header'''
    hlen = struct.calcsize(hfmt)
    hdr = fobj.read(hlen)
    return None if len(hdr) != hlen else struct.unpack(hfmt, hdr)


def get_record(edict, rechdr, fobj):
    """Deserialize a trace record from a file
 into a tuple (event_num, timestamp, pid, arg1, ..., arg6)."""
    if rechdr is None:
        return None
    rec = (rechdr[0], rechdr[1], rechdr[3])
    if rechdr[0] != dropped_event_id:
        event_id = rechdr[0]
        event = edict[event_id]
        for type, name in event.args:
            if is_string(type):
                l = fobj.read(4)
                (len,) = struct.unpack('=L', l)
                s = fobj.read(len)
                rec = rec + (s,)
            else:
                (value,) = struct.unpack('=Q', fobj.read(8))
                rec = rec + (value,)
    else:
        (value,) = struct.unpack('=Q', fobj.read(8))
        rec = rec + (value,)
    return rec


def read_record(edict, fobj):
    """Deserialize a trace record from a file into a tuple
 (event_num, timestamp, pid, arg1, ..., arg6)."""
    rechdr = read_header(fobj, rec_header_fmt)
    return get_record(edict, rechdr, fobj)  # return tuple of record elements


def read_trace_header(fobj):
    """Read and verify trace file header"""
    header = read_header(fobj, log_header_fmt)
    if header is None or \
       header[0] != header_event_id or \
       header[1] != header_magic:
        raise ValueError('Not a valid trace file!')

    log_version = header[2]
    if log_version not in [0, 2, 3]:
        raise ValueError('Unknown version of tracelog format!')
    if log_version != 3:
        raise ValueError('Log format %d not supported with this QEMU release!'
                         % log_version)


def read_trace_records(edict, fobj):
    """Deserialize trace records from a file, yielding record tuples
 (event_num, timestamp, pid, arg1, ..., arg6)."""
    while True:
        rec = read_record(edict, fobj)
        if rec is None:
            break

        yield rec


class Analyzer(object):
    """A trace file analyzer which processes trace records.

    An analyzer can be passed to run() or process().  The begin() method is
    invoked, then each trace record is processed, and finally the end() method
    is invoked.

    If a method matching a trace event name exists, it is invoked to process
    that trace record.  Otherwise the catchall() method is invoked."""

    def begin(self):
        """Called at the start of the trace."""
        print()

    def catchall(self, event, rec, db):
        """Called if no specific method for processing a trace event has been found."""
        pass

    def end(self):
        """Called at the end of the trace."""
        pass


class Formatter(Analyzer):
    def __init__(self):
        self.last_timestamp = None

    def catchall(self, event, rec, stage):
        timestamp = rec[1]
        if self.last_timestamp is None:
            self.last_timestamp = timestamp
        self.last_timestamp = timestamp

        i = 3
        pid = -1
        size = -1
        addr = -1
        pc = -1
        lr = -1
        cpsr = -1
        for t, n in event.args:
            if n == 'pid':
                pid = rec[i]
            elif n == 'size':
                size = rec[i]
            elif n == 'addr':
                addr = rec[i]
            elif n == 'pc':
                pc = rec[i]
            elif n == 'lr':
                lr = rec[i]
            elif n == 'cpsr':
                cpsr = rec[i]
            i = i+1
        db_info.get(stage).add_trace_write_entry(timestamp, pid, size, addr, pc, lr, cpsr)


def process(events, log, analyzer, read_header, stage):

    """Invoke an analyzer on each event in a log."""
    if isinstance(events, str):
        events = _read_events(open(events, 'r'))
    if isinstance(log, str):
        log = open(log, 'rb')

    if read_header:
        read_trace_header(log)

    dropped_event = Event.build("Dropped_Event(uint64_t num_events_dropped)")
    edict = {dropped_event_id: dropped_event}

    for num, event in enumerate(events):
        edict[num] = event

    def build_fn(analyzer, event):
        if isinstance(event, str):
            return analyzer.catchall

        fn = getattr(analyzer, event.name, None)
        if fn is None:
            return analyzer.catchall

        event_argcount = len(event.args)
        fn_argcount = len(inspect.getargspec(fn)[0]) - 1
        if fn_argcount == event_argcount + 1:
            # Include timestamp as first argument
            return lambda _, rec: fn(*((rec[1:2],) + rec[3:3 + event_argcount]))
        elif fn_argcount == event_argcount + 2:
            # Include timestamp and pid
            return lambda _, rec: fn(*rec[1:3 + event_argcount])
        else:
            # Just arguments, no timestamp or pid
            return lambda _, rec: fn(*rec[3:3 + event_argcount])

    analyzer.begin()
    fn_cache = {}
    db_info.create(stage, "tracedb")
    for rec in read_trace_records(edict, log):
        event_num = rec[0]
        event = edict[event_num]
        if event_num not in fn_cache:
            fn_cache[event_num] = build_fn(analyzer, event)
        fn_cache[event_num](event, rec, stage)
    db_info.get(stage).flush_tracedb()


def process_and_import(events, rawtrace, stage):
    read_header = True
    events = _read_events(open(events, 'r'))
    process(events, rawtrace, Formatter(), read_header, stage)
