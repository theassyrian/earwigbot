# -*- coding: utf-8  -*-
#
# Copyright (C) 2009-2012 by Ben Kurtovic <ben.kurtovic@verizon.net>
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

import logging
import re

from earwigbot.commands import command_manager
from earwigbot.irc import IRCConnection, Data, BrokenSocketException
from earwigbot.config import config

__all__ = ["Frontend"]

class Frontend(IRCConnection):
    """
    EarwigBot's IRC Frontend Component

    The IRC frontend runs on a normal IRC server and expects users to interact
    with it and give it commands. Commands are stored as "command classes",
    subclasses of BaseCommand in classes/base_command.py. All command classes
    are automatically imported by commands/__init__.py if they are in
    commands/.
    """
    sender_regex = re.compile(":(.*?)!(.*?)@(.*?)\Z")

    def __init__(self):
        self.logger = logging.getLogger("earwigbot.frontend")
        cf = config.irc["frontend"]
        base = super(Frontend, self)
        base.__init__(cf["host"], cf["port"], cf["nick"], cf["ident"],
                      cf["realname"], self.logger)
        command_manager.load(self)
        self._connect()

    def _process_message(self, line):
        """Process a single message from IRC."""
        line = line.strip().split()
        data = Data(line)  # New Data instance to store info about this line

        if line[1] == "JOIN":
            data.nick, data.ident, data.host = self.sender_regex.findall(line[0])[0]
            data.chan = line[2]
            # Check for 'join' hooks in our commands:
            command_manager.check("join", data)

        elif line[1] == "PRIVMSG":
            data.nick, data.ident, data.host = self.sender_regex.findall(line[0])[0]
            data.msg = " ".join(line[3:])[1:]
            data.chan = line[2]

            if data.chan == config.irc["frontend"]["nick"]:
                # This is a privmsg to us, so set 'chan' as the nick of the
                # sender, then check for private-only command hooks:
                data.chan = data.nick
                command_manager.check("msg_private", data)
            else:
                # Check for public-only command hooks:
                command_manager.check("msg_public", data)

            # Check for command hooks that apply to all messages:
            command_manager.check("msg", data)

        # If we are pinged, pong back:
        elif line[0] == "PING":
            self.pong(line[1])

        # On successful connection to the server:
        elif line[1] == "376":
            # If we're supposed to auth to NickServ, do that:
            try:
                username = config.irc["frontend"]["nickservUsername"]
                password = config.irc["frontend"]["nickservPassword"]
            except KeyError:
                pass
            else:
                msg = "IDENTIFY {0} {1}".format(username, password)
                self.say("NickServ", msg)

            # Join all of our startup channels:
            for chan in config.irc["frontend"]["channels"]:
                self.join(chan)
