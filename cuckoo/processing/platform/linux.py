# Copyright (C) 2010-2013 Claudio Guarnieri.
# Copyright (C) 2014-2016 Cuckoo Foundation.
# This file is part of Cuckoo Sandbox - http://www.cuckoosandbox.org
# See the file 'docs/LICENSE' for copying permission.

import os
import logging
import datetime
import re

import dateutil.parser

from cuckoo.common.abstracts import BehaviorHandler

log = logging.getLogger(__name__)

class FilteredProcessLog(list):
    def __init__(self, eventstream, **kwfilters):
        self.eventstream = eventstream
        self.kwfilters = kwfilters

    def __iter__(self):
        for event in self.eventstream:
            for k, v in self.kwfilters.items():
                if event[k] != v:
                    continue

                del event["type"]
                yield event

    def __nonzero__(self):
        return True

class LinuxSystemTap(BehaviorHandler):
    """Parses systemtap generated plaintext logs (see data/strace.stp)."""

    key = "processes"

    def __init__(self, *args, **kwargs):
        super(LinuxSystemTap, self).__init__(*args, **kwargs)

        self.processes = []
        self.pids_seen = set()
        self.forkmap = {}
        self.matched = False

        self._check_for_probelkm()

    def _check_for_probelkm(self):
        path_lkm = os.path.join(self.analysis.logs_path, "all.lkm")
        if os.path.exists(path_lkm):
            lines = open(path_lkm).readlines()

            forks = [re.findall("task (\d+)@0x[0-9a-f]+ forked to (\d+)@0x[0-9a-f]+", line) for line in lines]
            self.forkmap = dict((j, i) for i, j in reduce(lambda x, y: x+y, forks, []))

            # self.results["source"].append("probelkm")

    def handles_path(self, path):
        if path.endswith(".stap"):
            self.matched = True
            return True

    def parse(self, path):
        parser = StapParser(open(path))

        for event in parser:
            pid = event["pid"]
            if pid not in self.pids_seen:
                self.pids_seen.add(pid)
                ppid = self.forkmap.get(pid, -1)

                process = {
                    "pid": pid,
                    "ppid": ppid,
                    "process_name": event["process_name"],
                    "first_seen": event["time"],
                }

                # create a process event as we don't have those with linux+systemtap
                pevent = dict(process)
                pevent["type"] = "process"
                yield pevent

                process["calls"] = FilteredProcessLog(parser, pid=pid)
                self.processes.append(process)

            yield event

    def run(self):
        if not self.matched:
            return

        self.processes.sort(key=lambda process: process["first_seen"])
        return self.processes

class StapParser(object):
    """Handle .stap logs from the Linux analyzer."""

    def __init__(self, fd):
        self.fd = fd

    def __iter__(self):
        self.fd.seek(0)

        for line in self.fd:
            # 'Thu May  7 14:58:43 2015.390178 python@7f798cb95240[2114] close(6) = 0\n'
            # datetime is 31 characters
            datetimepart, r = line[:31], line[32:]

            # incredibly sophisticated date time handling
            dtms = datetime.timedelta(0, 0, int(datetimepart.split(".", 1)[1]))
            dt = dateutil.parser.parse(datetimepart.split(".", 1)[0]) + dtms

            parts = list()
            for delim in ("@", "[", "]", "(", ")", "= ", " (", ")"):
                part, _, r = r.strip().partition(delim)
                parts.append(part)

            pname, ip, pid, fn, args, _, retval, ecode = parts
            arguments = dict()

            n_args = 0
            while args:
                args = args.strip(", ")
                if self.is_array(args):
                    arg, _, args = args[1:].partition("]")
                    arg = [self.parse_arg(a) for a in self.split_array(arg)]
                else:
                    delim = "\", " if self.is_string(args) else ", "
                    arg, _, args = args.partition(delim)
                    arg = self.parse_arg(arg)

                arguments["p%u" % n_args] = arg
                n_args += 1

            pid = int(pid) if pid.isdigit() else -1

            yield {
                "time": dt, "process_name": pname, "pid": pid,
                "instruction_pointer": ip, "api": fn, "arguments": arguments,
                "return_value": retval, "status": ecode,
                "type": "apicall", "raw": line,
            }

    def parse_arg(self, arg):
        if self.is_string(arg):
            arg = arg[1:].decode('string_escape')
        return arg

    def split_array(self, arg):
        if self.is_string(arg):
            return arg.strip("\"").split("\", \"")
        else:
            return arg.split(", ")

    def is_array(self, arg):
        # TODO: expand collapsed varlist in strace.stp
        return arg.startswith("[") and not arg.startswith("[/*")

    def is_string(self, arg):
        return arg.startswith("\"")