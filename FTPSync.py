# Copyright (c) 2012 Jiri "NoxArt" Petruzelka
#
# Permission is hereby granted, free of charge, to any person
# obtaining a copy of this software and associated documentation
# files (the "Software"), to deal in the Software without
# restriction, including without limitation the rights to use,
# copy, modify, merge, publish, distribute, sublicense, and/or sell
# copies of the Software, and to permit persons to whom the
# Software is furnished to do so, subject to the following
# conditions:
#
# The above copyright notice and this permission notice shall be
# included in all copies or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND,
# EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES
# OF MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE AND
# NONINFRINGEMENT. IN NO EVENT SHALL THE AUTHORS OR COPYRIGHT
# HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER LIABILITY,
# WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING
# FROM, OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR
# OTHER DEALINGS IN THE SOFTWARE.

import sublime
import sublime_plugin
import shutil
import os
import hashlib
import json
import threading
import re
from ftpsyncwrapper import CreateConnection

# Init

# global config
settings = sublime.load_settings('ftpsync.sublime-settings')

isDebug = settings.get('debug')  # print debug messages to console?
isDebugVerbose = settings.get('debug_verbose')  # print overly informative messages?
projectDefaults = settings.get('project_defaults')  # default config for a project
ignore = settings.get('ignore')  # global ignore pattern

# loaded project's config will be merged with this global one
coreConfig = {
    'ignore': ignore,
    'connection_timeout': settings.get('connection_timeout')
}

# compiled global ignore pattern
re_ignore = re.compile(ignore)


# storing literals
configName = 'ftpsync.settings'
defaultConnectioConfigName = 'ftpsync.sublime-settings'
messageTimeout = 250
nestingLimit = 30

# connection cache pool
connections = {}
# threads pool
threads = []
# individual folder configs, file => config path
configs = {}
# messages scheduled to be dumped to status bar
messages = []


# ==== Messaging ===========================================================================
def statusMessage(text):
    sublime.status_message(text)


def dumpMessages():
    messages.reverse()
    for message in messages:
        statusMessage(message)
        messages.remove(message)


# ==== File&folders ========================================================================

def getFolders(viewOrFilename):
    if type(viewOrFilename) == str or type(viewOrFilename) == unicode:
        folders = []
        max = nestingLimit

        while True:
            split = os.path.split(viewOrFilename)
            viewOrFilename = split[0]
            max -= 1

            if len(split[1]) == 0 or max < 0:
                break

            folders.append(split[0])

        return folders
    else:
        return viewOrFilename.window().folders()


def findFile(folders, file_name):
    for folder in folders:
        if os.path.exists(os.path.join(folder, file_name)) is True:
            return folder

    return None


# ==== Config =============================================================================

# Invalidates all config cache entries belonging to a certain directory
# as long as they're empty or less nested in the filesystem
def invalidateConfigCache(config_dir_name):
    for file_name in configs:
        if file_name.startswith(config_dir_name) and (configs[file_name] is None or config_dir_name.startswith(configs[file_name])):
            configs.remove(configs[file_name])


# Finds a config file in given folders
def findConfigFile(folders):
    return findFile(folders, configName)


# Returns configuration file for a given file
def getConfigFile(file_name):
    # try cached
    try:
        if configs[file_name] and isDebug and isDebugVerbose:
            print "FTPSync > Loading config: cache hit (key: " + file_name + ")"

        return configs[file_name]
    except KeyError:
        try:
            folders = getFolders(file_name)

            if folders is None or len(folders) == 0:
                return None

            configFolder = findConfigFile(folders)

            if configFolder is None:
                if isDebug:
                    print "FTPSync > Found no config > for file: " + file_name

                return

            config = os.path.join(configFolder, configName)
            configs[file_name] = config
            return config

        except AttributeError:
            return None


def getConfigHash(file_name):
    return hashlib.md5(file_name).hexdigest()


# Parses given config and adds default values to each connection entry
def loadConfig(file_name):
    file = open(file_name)
    contents = ""

    for line in file:
        if line.find('//') is -1:
            contents += line

    try:
        config = json.loads(contents)
    except:
        if isDebug:
            print "FTPSync > Failed parsing configuration file: " + file_name

        messages.append("FTPSync > Failed parsing configuration file " + file_name + " (commas problem?)")
        sublime.set_timeout(dumpMessages, messageTimeout)
        return None

    result = {}

    for name in config:
        result[name] = dict(projectDefaults.items() + config[name].items())
        result[name]['file_name'] = file_name

    final = dict(coreConfig.items() + {"connections": result}.items())

    return final


# ==== Remote =============================================================================

# Returns connection, connects if needed
def getConnection(hash, config):
    try:
        if connections[hash] and isDebug and isDebugVerbose:
            print "FTPSync > Connection cache hit (key: " + hash + ")"

        return connections[hash]
    except KeyError:
        connections[hash] = []

        for name in config['connections']:
            properties = config['connections'][name]

            # 1. initialize
            connection = CreateConnection(properties, name)

            # 2. connect
            try:
                connection.connect()
            except:
                if isDebug:
                    print "FTPSync [" + name + "] > Connection failed"

                messages.append("FTPSync [" + name + "] > Connection failed")
                sublime.set_timeout(dumpMessages, messageTimeout)

                connection.close(hash)

            if isDebug:
                print "FTPSync [" + name + "] > Connected to: " + properties['host'] + ":" + str(properties['port']) + " (timeout: " + str(properties['timeout']) + ") (key: " + hash + ")"

            # 3. authenticate
            if connection.authenticate() and isDebug:
                print "FTPSync [" + name + "] > Authentication processed"

            # 4. login
            if properties['username'] is not None:
                connection.login()

                if isDebug:
                    pass_present = " (using password: NO)"
                    if len(properties['password']) > 0:
                        pass_present = " (using password: YES)"

                    print "FTPSync [" + name + "] > Logged in as: " + properties['username'] + pass_present

            elif isDebug:
                print "FTPSync [" + name + "] > Anonymous connection"

            # 5. set initial directory, set name, store connection
            try:
                connection.cwd(properties['path'])

                connections[hash].append(connection)
            except:
                if isDebug:
                    print "FTPSync [" + name + "] > Failed to set path (probably connection failed)"

        # schedule connection timeout
        def closeThisConnection():
            closeConnection(hash, config)

        sublime.set_timeout(closeThisConnection, config['connection_timeout'] * 1000)

        # return all connections
        return connections[hash]


# Close all connections for a given config file
def closeConnection(hash, config):
    try:
        for connection in connections[hash]:
            connection.close(connections, hash)

            if isDebug:
                print "FTPSync [" + connection.name + "] > closed"

        if len(connections[hash]) == 0:
            connections.pop(hash)

    except Exception, e:
        print e

        return


# Uploads given file
def performSync(file_name, config_file, disregardIgnore=False):
    config = loadConfig(config_file)
    basename = os.path.basename(file_name)

    if disregardIgnore is False and len(ignore) > 0 and re_ignore.search(file_name) is not None:
        if isDebug and isDebugVerbose:
            print "FTPSync > file globally ignored: " + basename

        return

    connections = getConnection(getConfigHash(config_file), config)
    index = -1
    stored = []
    failed = False

    for name in config['connections']:
        index += 1

        if disregardIgnore is False and config['connections'][name]['ignore'] is not None and re.search(config['connections'][name]['ignore'], file_name):
            if isDebug and isDebugVerbose:
                print "FTPSync [" + name + "] > file ignored by rule: " + basename

            break

        try:
            uploaded = connections[index].put(file_name)

            if type(uploaded) is str or type(uploaded) is unicode:
                stored.append(uploaded)

                if isDebug:
                    print "FTPSync [" + name + "] > uploaded " + basename

            else:
                failed = type(uploaded)

        except Exception, e:
            failed = e

        if failed:
            if isDebug:
                print "FTPSync [" + name + "] > upload failed: (" + basename + ") " + str(failed)

            messages.append("FTPSync [" + name + "] > upload failed: " + basename)

            sublime.set_timeout(dumpMessages, messageTimeout)

    if len(stored) > 0:
        messages.append("FTPSync [remotes: " + ",".join(stored) + "] > uploaded " + basename)

        sublime.set_timeout(dumpMessages, messageTimeout)


# File watching
class RemoteSync(sublime_plugin.EventListener):
    def on_post_save(self, view):
        file_name = view.file_name()
        thread = RemoteSyncCall(file_name, getConfigFile(file_name))
        threads.append(thread)
        thread.start()

    def on_close(self, view):
        config_file = getConfigFile(view.file_name())

        if config_file is not None:
            hash = getConfigHash(config_file)
            closeConnection(hash)


# Remote handling
class RemoteSyncCall(threading.Thread):
    def __init__(self, file_name, config, disregardIgnore=False):
        self.file_name = file_name
        self.config = config
        self.disregardIgnore = disregardIgnore
        threading.Thread.__init__(self)

    def run(self):
        if self.config is None:
            return False

        performSync(self.file_name, self.config, self.disregardIgnore)


# Sets up a config file in a directory
class NewFtpSyncCommand(sublime_plugin.TextCommand):
    def run(self, edit, dirs):
        if len(dirs) == 0:
            dirs = [os.path.dirname(self.view.file_name())]

        default = os.path.join(sublime.packages_path(), 'FTPSync', defaultConnectioConfigName)

        for dir in dirs:
            config = os.path.join(dir, configName)

            invalidateConfigCache(dir)

            if os.path.exists(config) is True:
                self.view.window().open_file(config)
            else:
                shutil.copyfile(default, config)
                self.view.window().open_file(config)


# Synchronize selected file/directory
class FtpSyncTarget(sublime_plugin.TextCommand):
    def run(self, edit, paths):
        for target in paths:
            if os.path.isfile(target):
                thread = RemoteSyncCall(target, getConfigFile(target), True)
                threads.append(thread)
                thread.start()
