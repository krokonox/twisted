
# Twisted, the Framework of Your Internet
# Copyright (C) 2001 Matthew W. Lefkowitz
#
# This library is free software; you can redistribute it and/or
# modify it under the terms of version 2.1 of the GNU Lesser General Public
# License as published by the Free Software Foundation.
#
# This library is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the GNU
# Lesser General Public License for more details.
#
# You should have received a copy of the GNU Lesser General Public
# License along with this library; if not, write to the Free Software
# Foundation, Inc., 59 Temple Place, Suite 330, Boston, MA  02111-1307  USA


# System Imports
import types
import os
import time

# Twisted Import
from twisted.python.runtime import platform

if platform.getType() != 'java':
    import signal


import sys
import socket
CONNECTION_LOST = -1
CONNECTION_DONE = -2

theApplication = None

# Twisted Imports

from twisted.python import threadable, log, delay
from twisted.persisted import styles
from twisted.python.defer import Deferred, DeferredList

# Sibling Imports

theTimeouts = delay.Time() # A delay for non-peristent delayed actions

def addTimeout(method, seconds):
    """Add a method which will time out after a given interval.

    The given method will always time out before a server shuts down,
    and will never persist.
    """
    theTimeouts.runLater(seconds, method)


class DummyResolver:
    """
    An implementation of a synchronous resolver, from Python's socket stuff.
    This may be ill-placed.
    """
    def resolve(self, deferred, name, type=1, timeout=10):
        if type != 1:
            deferred.errback("type not supportded")
            return
        try:
            address = socket.gethostbyname(name)
        except socket.error:
            deferred.errback("address not found")
        else:
            deferred.callback(address)

reads = {}
writes = {}
running = None
shuttingDown = None
delayeds = [theTimeouts]
beforeShutdown = []
duringShutdown = []
afterShutdown = []
resolver = DummyResolver()
interruptCountdown = 5

def shutDown(*ignored):
    """Run all shutdown callbacks (save all running Applications) and exit.

    This is called by various signal handlers which should cause
    the process to exit.  It can also be called directly in order
    to trigger a clean shutdown.
    """
    global running, interruptCountdown, shuttingDown
    if not shuttingDown:
        if threadable.threaded:
            removeReader(waker)
        shuttingDown = 1
        log.msg('Starting shutdown sequence.')
        defrList = []
        for callback in beforeShutdown:
            try:
                d = callback()
            except:
                log.deferr()
            else:
                if isinstance(d, Deferred):
                    defrList.append(d)
        if defrList:
            DeferredList(defrList).addCallbacks(stopMainLoop, stopMainLoop).arm()
        else:
            stopMainLoop()
    elif interruptCountdown > 0:
        log.msg('Raising exception in %s more interrupts!' % interruptCountdown)
        interruptCountdown = interruptCountdown - 1
    else:
        stopMainLoop()
        raise RuntimeError("Shut down exception!")

def stopMainLoop(*ignored):
    global running
    running = 0
    log.msg("Stopping main loop.")

def runUntilCurrent():
    """Run all delayed loops and return a timeout for when the next call expects to be made.
    """
    # This code is duplicated for efficiency later.
    timeout = None
    for delayed in delayeds:
        delayed.runUntilCurrent()
    for delay in delayeds:
        newTimeout = delayed.timeout()
        if ((newTimeout is not None) and
            ((timeout is None) or
             (newTimeout < timeout))):
            timeout = newTimeout
    return timeout

def iterate(timeout=0.):
    """Do one iteration of the main loop.

    I will run any simulated (delayed) code, and process any pending I/O.
    I will not block.  This is meant to be called from a high-freqency
    updating loop function like the frame-processing function of a game.
    """
    for delayed in delayeds:
        delayed.runUntilCurrent()
    doSelect(timeout)


def handleSignals():
    """Install the signal handlers for the Twisted event loop."""
    signal.signal(signal.SIGINT, shutDown)
    signal.signal(signal.SIGTERM, shutDown)

    # Catch Ctrl-Break in windows (only available in 2.2b1 onwards)
    if hasattr(signal, "SIGBREAK"):
        signal.signal(signal.SIGBREAK, shutDown)

    if platform.getType() == 'posix':
        signal.signal(signal.SIGCHLD, process.reapProcess)


def run(installSignalHandlers=1):
    """Run input/output and dispatched/delayed code.

    This call \"never\" returns.  It is the main loop which runs delayed timers
    (see twisted.python.delay and addDelayed), and the I/O monitor (doSelect).

    """
    # now this is an ugly hack - make sure that we have a reactor installed
    import twisted.internet
    if not twisted.internet.reactor:
        import default
        reactor = default.DefaultSelectReactor()
        reactor.install()
    
    global running
    running = 1
    threadable.registerAsIOThread()

    callDuringShutdown(disconnectAll)

    if installSignalHandlers:
        handleSignals()

    for function in _whenRunning:
        function()
    _whenRunning[:] = []
    try:
        try:
            while running:
                # Advance simulation time in delayed event
                # processors.
                timeout = None
                for delayed in delayeds:
                    delayed.runUntilCurrent()
                for delayed in delayeds:
                    newTimeout = delayed.timeout()
                    if ((newTimeout is not None) and
                        ((timeout is None) or
                         (newTimeout < timeout))):
                        timeout = newTimeout

                doSelect(running and timeout)
        except:
            log.msg("Unexpected error in main loop.")
            log.deferr()
            shutDown()
            raise
        else:
            log.msg('Main loop terminated.')

    finally:
        for callback in duringShutdown + afterShutdown:
            try:
                callback()
            except:
                log.deferr()


def disconnectAll():
    """Disconnect every reader, and writer in the system.
    """
    selectables = removeAll()
    for reader in selectables:
        log.logOwner.own(reader)
        try:
            reader.connectionLost()
        except:
            log.deferr()
        log.logOwner.disown(reader)

_whenRunning = []

def callWhenRunning(function):
    """Add a function to be called when the system starts running.

    If the system is already running, then the function runs immediately.  If
    the system has not yet started running, the function will be queued to get
    run when the mainloop starts.
    """
    if running:
        function()
    else:
        _whenRunning.append(function)

def callBeforeShutdown(function):
    """Add a function to be called before shutdown begins.

    These functions are tasks to be performed in order to run a
    "clean" shutdown.  This may involve tasks that keep the mainloop
    running, so any function registered in this list may return a
    Deferred, which will delay the actual shutdown until later.
    """
    beforeShutdown.append(function)

def removeCallBeforeShutdown(function):
    """Remove a function registered with callBeforeShutdown.
    """
    beforeShutdown.remove(function)

def callDuringShutdown(function):
    """Add a function to be called during shutdown.

    These functions ought to shut down the event loop -- stopping
    thread pools, closing down all connections, etc.
    """
    duringShutdown.append(function)

def removeCallDuringShutdown(function):
    duringShutdown.remove(function)

def callAfterShutdown(function):
    afterShutdown.append(function)

def removeCallAfterShutdown(function):
    duringShutdown.remove(function)

def addDelayed(delayed):
    """Add an object implementing the IDelayed interface to the event loop.

    See twisted.python.delay.IDelayed for more details.
    """
    delayeds.append(delayed)

def removeDelayed(delayed):
    """Remove a Delayed object from the event loop.
    """
    delayeds.remove(delayed)


# Sibling Import
import process

# Work on Jython
if platform.getType() == 'java':
    import jnternet

# backward compatibility stuff
import app
Application = app.Application

