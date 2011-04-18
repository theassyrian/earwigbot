# -*- coding: utf-8  -*-

## Imports
import re

from config.irc_config import *
from config.secure_config import *

from irc import triggers
from irc.connection import Connection
from irc.data import Data

def get_connection():
    connection = Connection(HOST, PORT, NICK, IDENT, REALNAME)
    return connection

def main(connection):
    connection.connect()
    read_buffer = str()

    while 1:
        try:        
            read_buffer = read_buffer + connection.get()
        except RuntimeError: # socket broke
            print "socket has broken on front-end; restarting bot..."
            return

        lines = read_buffer.split("\n")
        read_buffer = lines.pop()

        for line in lines:
            line = line.strip().split()
            data = Data()

            if line[1] == "JOIN":
                data.nick, data.ident, data.host = re.findall(":(.*?)!(.*?)@(.*?)\Z", line[0])[0]
                data.chan = line[2][1:]

                triggers.check(connection, data, "join") # check if there's anything we can respond to, and if so, respond

            if line[1] == "PRIVMSG":
                data.nick, data.ident, data.host = re.findall(":(.*?)!(.*?)@(.*?)\Z", line[0])[0]
                data.msg = ' '.join(line[3:])[1:]
                data.chan = line[2]

                if data.chan == NICK: # this is a privmsg to us, so set 'chan' as the nick of the sender
                    data.chan = data.nick
                    triggers.check(connection, data, "msg_private") # only respond if it's a private message
                else:
                    triggers.check(connection, data, "msg_public") # only respond if it's a public (channel) message

                triggers.check(connection, data, "msg") # check for general messages

                if data.msg.startswith("!restart"): # hardcode the !restart command (we can't restart from within an ordinary command)
                    if data.host in OWNERS:
                        print "restarting bot per owner request..."
                        return

            if line[0] == "PING": # If we are pinged, pong back to the server
                connection.send("PONG %s" % line[1])

            if line[1] == "376":
                if NS_AUTH: # if we're supposed to auth to nickserv, do that
                    connection.say("NickServ", "IDENTIFY %s %s" % (NS_USER, NS_PASS))
                for chan in CHANS: # join all of our startup channels
                    connection.join(chan)
