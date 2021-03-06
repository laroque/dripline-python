import re
import socket
import threading

import scarab

from dripline.core import Service
# Dripline Exceptions currently unavailable

# logging currently unavailable
# import logging
# logger = logging.getLogger(__name__)

__all__ = []


__all__.append('EthernetSCPIService')
class EthernetSCPIService(Service):
    '''
    A fairly generic subclass of Service for connecting to ethernet-capable instruments/devices.
    In is developed for and tested against devices with a SCPI-compliant command set, but may be usable with devices which do not strictly conform.
    In particular, devices must support sending a response to every command received (either natively, or via SCPI's command composition) and responses are expected to include a termination marking complete transmission.
    '''
    def __init__(self,
                 socket_timeout=1.0,
                 socket_info=('localhost', 1234),
                 cmd_at_reconnect=['*OPC?'],
                 reconnect_test='1',
                 command_terminator='',
                 response_terminator=None,
                 reply_echo_cmd=False,
                 **kwargs
                 ):
        '''
        socket_timeout (int) : number of seconds to wait for a reply from the device before timeout
        socket_info (tuple or string) : either socket.socket.connect argument tuple, or string that parses into one
        cmd_at_reconnect ([str,...]) : a list of commands to send to the device every time the socket connection is estabilished
                                       note that these will be sent on *every* connection, which may be disruptive to ongoing activity
        reconnect_test (str) : expected return from the last command in the cmd_at_reconnect list, must match exactly or the reconnect
                               is deemed a failure
        command_terminator (str) : string to be post-pended to commands, indicating to the device that the transmission is complete (often \r, \n, or \r\n)
        response_terminator (str) : string added to the end of a reply from the device, indicates the end of the reply
        reply_echo_cmd (bool) : indicates that the device includes the the received command in its reply
        '''
        Service.__init__(self, **kwargs)

        if isinstance(socket_info, str):
            # logger.debug
            print("Formatting socket_info: {}".format(socket_info))
            re_str = "\([\"'](\S+)[\"'], ?(\d+)\)"
            (ip,port) = re.findall(re_str,socket_info)[0]
            socket_info = (ip,int(port))
        if response_terminator is None or response_terminator == '':
            raise ValueError("Invalid response terminator: <{}>! Expect string".format(repr(response_terminator)))
        if not isinstance(cmd_at_reconnect, list) or len(cmd_at_reconnect)==0:
            if cmd_at_reconnect is not None:
                raise ValueError("Invalid cmd_at_reconnect: <{}>! Expect non-zero length list".format(repr(cmd_at_reconnect)))

        self.alock = threading.Lock()
        self.socket = socket.socket()
        self.socket_timeout = float(socket_timeout)
        self.socket_info = socket_info
        self.cmd_at_reconnect = cmd_at_reconnect
        self.reconnect_test = reconnect_test
        self.command_terminator = command_terminator
        self.response_terminator = response_terminator
        self.reply_echo_cmd = reply_echo_cmd

        self._reconnect()

    def _reconnect(self):
        '''
        Method establishing socket to ethernet instrument.
        Called by __init__ or send (on failed communication).
        '''
        self.socket.close()
        self.socket = socket.socket()
        try:
            self.socket = socket.create_connection(self.socket_info, self.socket_timeout)
        except (socket.error, socket.timeout) as err:
            # logger.warning
            print("connection {} refused: {}".format(self.socket_info, err))
            # exception.DriplineHardwareConnectionError
            raise Exception("Unable to establish ethernet socket {}".format(self.socket_info))
        # logger.info
        print("Ethernet socket {} established".format(self.socket_info))

        # Lantronix xDirect adapters have no query options
        if self.cmd_at_reconnect is None:
            return
        commands = self.cmd_at_reconnect[:]
        # Agilent/Keysight instruments give an unprompted *IDN? response on
        #   connection. This must be cleared before communicating with a blank
        #   listen or all future queries will be offset.
        while commands[0] is None:
            # logger.debug
            print("Emptying reconnect buffer")
            commands.pop(0)
            self._listen(blank_command=True)
        response = self._send_commands(commands)
        # Final cmd_at_reconnect should return '1' to test connection.
        if response[-1] != self.reconnect_test:
            self.socket.close()
            # logger.warning
            print("Failed connection test.  Response was {}".format(response))
            # exceptions.DriplineHardwareConnectionError
            raise Exception("Failed connection test.")


    def send_to_device(self, commands, **kwargs):
        '''
        Standard device access method to communicate with instrument.
        NEVER RENAME THIS METHOD!

        commands (list||None): list of command(s) to send to the instrument following (re)connection to the instrument, still must return a reply!
                             : if impossible, set as None to skip
        '''
        if isinstance(commands, str):
            commands = [commands]
        self.alock.acquire()

        try:
            data = self._send_commands(commands)
        except socket.error as err:
            # logger.warning
            print("socket.error <{}> received, attempting reconnect".format(err))
            self._reconnect()
            data = self._send_commands(commands)
            # logger.critical
            print("Ethernet connection reestablished")
        # exceptions.DriplineHardwareResponselessError
        except Exception as err:
            # logger.critical
            print(str(err))
            try:
                self._reconnect()
                data = self._send_commands(commands)
                # logger.critical
                print("Query successful after ethernet connection recovered")
            # exceptions.DriplineHardwareConnectionError
            except socket.error: # simply trying to make it possible to catch the error below
                # logger.critical
                print("Ethernet reconnect failed, dead socket")
                # exceptions.DriplineHardwareConnectionError
                raise socket.error("Broken ethernet socket")
            # exceptions.DriplineHardwareResponselessError
            except Exception as err:
                # logger.critical
                print("Query failed after successful ethernet socket reconnect")
                # exceptions.DriplineHardwareResponselessError
                raise Exception(err)
        finally:
            self.alock.release()
        to_return = ';'.join(data)
        # logger.debug
        print("should return:\n{}".format(to_return))
        return to_return


    def _send_commands(self, commands):
        '''
        Take a list of commands, send to instrument and receive responses, do any necessary formatting.

        commands (list||None): list of command(s) to send to the instrument following (re)connection to the instrument, still must return a reply!
                             : if impossible, set as None to skip
        '''
        all_data=[]

        for command in commands:
            command += self.command_terminator
            # logger.debug
            print("sending: {}".format(command.encode()))
            self.socket.send(command.encode())
            if command == self.command_terminator:
                blank_command = True
            else:
                blank_command = False

            data = self._listen(blank_command)

            if self.reply_echo_cmd:
                if data.startswith(command):
                    data = data[len(command):]
                elif not blank_command:
                    raise ThrowReply('device_error_connection', "Bad ethernet query return: {}".format(data))
            # logger.info
            print("sync: {} -> {}".format(repr(command),repr(data)))
            all_data.append(data)
        return all_data


    def _listen(self, blank_command=False):
        '''
        Query socket for response.

        blank_comands (bool): flag which is True when command is exactly the command terminator
        '''
        data = ''
        try:
            while True:
                data += self.socket.recv(1024).decode(errors='replace')
                if data.endswith(self.response_terminator):
                    terminator = self.response_terminator
                    break
                # Special exception for disconnect of prologix box to avoid infinite loop
                if data == '':
                    raise ThrowReply('device_error_no_response', "Empty socket.recv packet")
        except socket.timeout:
            # logger.warning
            print("socket.timeout condition met; received:\n{}".format(repr(data)))
            if blank_command == False:
                # exceptions.DriplineHardwareResponselessError
                raise ThrowReply('device_error_no_response', "Unexpected socket.timeout")
            terminator = ''
        # logger.debug
        print(repr(data))
        data = data[0:data.rfind(terminator)]
        return data
