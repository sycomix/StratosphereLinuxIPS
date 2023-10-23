# Stratosphere Linux IPS. A machine-learning Intrusion Detection System
# Copyright (C) 2021 Sebastian Garcia

# This program is free software; you can redistribute it and/or
# modify it under the terms of the GNU General Public License
# as published by the Free Software Foundation; either version 2
# of the License, or (at your option) any later version.

# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.

# You should have received a copy of the GNU General Public License
# along with this program; if not, write to the Free Software
# Foundation, Inc., 51 Franklin Street, Fifth Floor, Boston, MA  02110-1301, USA.
# Contact: eldraco@gmail.com, sebastian.garcia@agents.fel.cvut.cz, stratosphere@aic.fel.cvut.cz
import multiprocessing
import json
from datetime import datetime
from datetime import timedelta
import sys
import configparser
from slips.core.database import __database__
import time
import ipaddress
import traceback
import os
import binascii
import base64


def timeit(method):
    def timed(*args, **kw):
        ts = time.time()
        result = method(*args, **kw)
        te = time.time()
        if 'log_time' in kw:
            name = kw.get('log_name', method.__name__.upper())
            kw['log_time'][name] = int((te - ts) * 1000)
        else:
            print(f'\t\033[1;32;40mFunction {method.__name__}() took {(te - ts) * 1000:2.2f}ms\033[00m')
        return result
    return timed


# Profiler Process
class ProfilerProcess(multiprocessing.Process):
    """ A class to create the profiles for IPs and the rest of data """
    def __init__(self, inputqueue, outputqueue, config, width):
        self.name = 'Profiler'
        multiprocessing.Process.__init__(self)
        self.inputqueue = inputqueue
        self.outputqueue = outputqueue
        self.config = config
        self.width = width
        self.columns_defined = False
        self.timeformat = None
        self.input_type = False
        # Read the configuration
        self.read_configuration()
        # Start the DB
        __database__.start(self.config)
        # Set the database output queue
        __database__.setOutputQueue(self.outputqueue)
        # 1st. Get the data from the interpreted columns
        self.id_separator = __database__.getFieldSeparator()

    def print(self, text, verbose=1, debug=0):
        """
        Function to use to print text using the outputqueue of slips.
        Slips then decides how, when and where to print this text by taking all the prcocesses into account

        Input
         verbose: is the minimum verbosity level required for this text to be printed
         debug: is the minimum debugging level required for this text to be printed
         text: text to print. Can include format like 'Test {}'.format('here')

        If not specified, the minimum verbosity level required is 1, and the minimum debugging level is 0
        """

        vd_text = str(int(verbose) * 10 + int(debug))
        self.outputqueue.put(f'{vd_text}|{self.name}|[{self.name}] {str(text)}')

    def read_configuration(self):
        """ Read the configuration file for what we need """
        # Get the home net if we have one from the config
        try:
            self.home_net = ipaddress.ip_network(self.config.get('parameters', 'home_network'))
        except (configparser.NoOptionError, configparser.NoSectionError, NameError):
            # There is a conf, but there is no option, or no section or no
            # configuration file specified
            self.home_net = False

        # Get the time window width, if it was not specified as a parameter
        if not self.width:
            try:
                data = self.config.get('parameters', 'time_window_width')
                self.width = float(data)
            except ValueError:
                # Its not a float
                if 'only_one_tw' in data:
                    # Only one tw. Width is 10 9s, wich is ~11,500 days, ~311 years
                    self.width = 9999999999
            except configparser.NoOptionError:
                # By default we use 3600 seconds, 1hs
                self.width = 3600
            except (configparser.NoOptionError, configparser.NoSectionError, NameError):
                # There is a conf, but there is no option, or no section or no
                # configuration file specified
                self.width = 3600
        else:
            self.width = 3600
        # Report the time window width
        if self.width == 9999999999:
            self.print(f'Time Windows Width used: {self.width} seconds. Only 1 time windows. Dates in the names of files are 100 years in the past.', 4, 0)
        else:
            self.print(f'Time Windows Width used: {self.width} seconds.', 4, 0)

        # Get the format of the time in the flows
        try:
            self.timeformat = self.config.get('timestamp', 'format')
        except (configparser.NoOptionError, configparser.NoSectionError, NameError):
            # There is a conf, but there is no option, or no section or no configuration file specified
            # This has to be None, beacause we try to detect the time format below, if it is None.
            self.timeformat = None
        ##
        # Get the direction of analysis
        try:
            self.analysis_direction = self.config.get('parameters', 'analysis_direction')
        except (configparser.NoOptionError, configparser.NoSectionError, NameError):
            # There is a conf, but there is no option, or no section or no configuration file specified
            # By default
            self.analysis_direction = 'all'
        # Get the default label for all this flow. Used during training usually
        try:
            self.label = self.config.get('parameters', 'label')
        except (configparser.NoOptionError, configparser.NoSectionError, NameError):
            # There is a conf, but there is no option, or no section or no configuration file specified
            # By default
            self.label = 'unknown'

    def define_type(self, line):
        """
        Try to define very fast the type of input
        Heuristic detection: dict (zeek from pcap of int), json (suricata), or csv (argus), or TAB separated (conn.log only from zeek)?
        Bro actually gives us json, but it was already coverted into a dict
        in inputProcess
        Outputs can be: zeek, suricata, argus, zeek-tabs
        """
        try:
            # All lines come as a dict, specifying the name of file and data.
            # Take the data
            try:
                # Did data came with the json format?
                data = line['data']
                # For now we dont use the file type, but is handy for the future
                file_type = line['type']
                # Yes
            except KeyError:
                # No
                data = line
                self.print('\tData did not arrived in json format from the input', 0, 1)
                self.print('\tProblem in define_type()', 0, 1)
                return False

            # In the case of Zeek from an interface or pcap,
            # the structure is a JSON
            # So try to convert into a dict
            if type(data) == dict:
                try:
                    _ = data['data']
                    self.separator = '	'
                    self.input_type = 'zeek-tabs'
                except KeyError:
                    self.input_type = 'zeek'
            else:
                try:
                    data = json.loads(data)
                    if data['event_type'] == 'flow':
                        self.input_type = 'suricata'
                except ValueError:
                    nr_commas = len(data.split(','))
                    nr_tabs = len(data.split('	'))
                    if nr_commas > nr_tabs:
                        # Commas is the separator
                        self.separator = ','
                        self.input_type = 'nfdump' if nr_commas > 40 else 'argus'
                    elif nr_tabs > nr_commas:
                        # Tabs is the separator
                        # Probably a conn.log file alone from zeek
                        self.separator = '	'
                        self.input_type = 'zeek-tabs'
        except Exception as inst:
            self.print('\tProblem in define_type()', 0, 1)
            self.print(str(type(inst)), 0, 1)
            self.print(str(inst), 0, 1)
            sys.exit(1)

    def define_columns(self, new_line):
        """ Define the columns for Argus and Zeek-tabs from the line received """
        # These are the indexes for later fast processing
        line = new_line['data']
        self.column_idx = {
            'starttime': False,
            'endtime': False,
            'dur': False,
            'proto': False,
            'appproto': False,
            'saddr': False,
            'sport': False,
            'dir': False,
            'daddr': False,
            'dport': False,
            'state': False,
            'pkts': False,
            'spkts': False,
            'dpkts': False,
            'bytes': False,
            'sbytes': False,
            'dbytes': False,
        }
        try:
            nline = line.strip().split(self.separator)
            for field in nline:
                if 'time' in field.lower():
                    self.column_idx['starttime'] = nline.index(field)
                elif 'dur' in field.lower():
                    self.column_idx['dur'] = nline.index(field)
                elif 'proto' in field.lower():
                    self.column_idx['proto'] = nline.index(field)
                elif 'srca' in field.lower():
                    self.column_idx['saddr'] = nline.index(field)
                elif 'sport' in field.lower():
                    self.column_idx['sport'] = nline.index(field)
                elif 'dir' in field.lower():
                    self.column_idx['dir'] = nline.index(field)
                elif 'dsta' in field.lower():
                    self.column_idx['daddr'] = nline.index(field)
                elif 'dport' in field.lower():
                    self.column_idx['dport'] = nline.index(field)
                elif 'state' in field.lower():
                    self.column_idx['state'] = nline.index(field)
                elif 'totpkts' in field.lower():
                    self.column_idx['pkts'] = nline.index(field)
                elif 'totbytes' in field.lower():
                    self.column_idx['bytes'] = nline.index(field)
                elif 'srcbytes' in field.lower():
                    self.column_idx['sbytes'] = nline.index(field)

            temp_dict = {
                i: self.column_idx[i]
                for i, value in self.column_idx.items()
                if type(value) != bool or self.column_idx[i] != False
            }
            self.column_idx = temp_dict
        except Exception as inst:
            self.print('\tProblem in define_columns()', 0, 1)
            self.print(str(type(inst)), 0, 1)
            self.print(str(inst), 0, 1)
            sys.exit(1)

    def define_time_format(self, time: str) -> str:
        time_format: str = None
        try:
            # Try unix timestamp in seconds.
            datetime.fromtimestamp(float(time))
            time_format = 'unixtimestamp'
        except ValueError:
            try:
                # Try the default time format for suricata.
                datetime.strptime(time, '%Y-%m-%dT%H:%M:%S.%f%z')
                time_format = '%Y-%m-%dT%H:%M:%S.%f%z'
            except ValueError:
                # Let's try the classic time format "'%Y-%m-%d %H:%M:%S.%f'"
                try:
                    datetime.strptime(time, '%Y-%m-%d %H:%M:%S.%f')
                    time_format = '%Y-%m-%d %H:%M:%S.%f'
                except ValueError:
                    try:
                        datetime.strptime(time, '%Y-%m-%d %H:%M:%S')
                        time_format = '%Y-%m-%d %H:%M:%S'
                    except ValueError:
                        try:
                            datetime.strptime(time, '%Y/%m/%d %H:%M:%S.%f')
                            time_format = '%Y/%m/%d %H:%M:%S.%f'
                        except ValueError:
                            # We did not find the right time format.
                            self.outputqueue.put("01|profiler|[Profile] We did not find right time format. Please set the time format in the configuration file.")
        return time_format

    def get_time(self, time: str) -> datetime:
        """
        Take time in string and return datetime object.
        The format of time can be completely different. It can be seconds, or dates with specific formats.
        If user does not define the time format in configuration file, we have to try most frequent cases of time formats.
        """

        if not self.timeformat:
            # The time format was not defined from configuration file neither from last flows.
            self.timeformat = self.define_time_format(time)

        defined_datetime: datetime = None
        if self.timeformat:
            if self.timeformat == 'unixtimestamp':
                # The format of time is in seconds.
                defined_datetime = datetime.fromtimestamp(float(time))
            else:
                try:
                    # The format of time is a complete date.
                    defined_datetime = datetime.strptime(time, self.timeformat)
                except ValueError:
                    defined_datetime = None
        else:
            # We do not know the time format so we can not read it.
            self.outputqueue.put(
                "01|profiler|[Profile] We did not find right time format. Please set the time format in the configuration file.")

        # if defined_datetime is None and self.timeformat:
            # There is suricata issue with invalid timestamp for examaple: "1900-01-00T00:00:08.511802+0000"
            # pass
        return defined_datetime

    def process_zeek_tabs_input(self, new_line: str) -> None:
        """
        Process the tab line from zeek.
        """
        line = new_line['data']
        line = line.rstrip()
        line = line.split('\t')

        # Generic fields in Zeek
        self.column_values: dict = {'type': ''}
        try:
            self.column_values['starttime'] = self.get_time(line[0])
        except IndexError:
            self.column_values['starttime'] = ''
        try:
            self.column_values['uid'] = line[1]
        except IndexError:
            self.column_values['uid'] = False
        try:
            self.column_values['saddr'] = line[2]
        except IndexError:
            self.column_values['saddr'] = ''
        try:
            self.column_values['daddr'] = line[4]
        except IndexError:
            self.column_values['daddr'] = ''

        if 'conn' in new_line['type']:
            self.column_values['type'] = 'conn'
            try:
                self.column_values['dur'] = float(line[8])
            except (IndexError, ValueError):
                self.column_values['dur'] = 0
            self.column_values['endtime'] = self.column_values['starttime'] + timedelta(
                seconds=self.column_values['dur'])
            self.column_values['proto'] = line[6]
            try:
                self.column_values['appproto'] = line[7]
            except IndexError:
                # no service recognized
                self.column_values['appproto'] = ''
            try:
                self.column_values['sport'] = line[3]
            except IndexError:
                self.column_values['sport'] = ''
            self.column_values['dir'] = '->'
            try:
                self.column_values['dport'] = line[5]
            except IndexError:
                self.column_values['dport'] = ''
            try:
                self.column_values['state'] = line[11]
            except IndexError:
                self.column_values['state'] = ''
            try:
                self.column_values['spkts'] = float(line[16])
            except (IndexError, ValueError):
                self.column_values['spkts'] = 0
            try:
                self.column_values['dpkts'] = float(line[18])
            except (IndexError, ValueError):
                self.column_values['dpkts'] = 0
            self.column_values['pkts'] = self.column_values['spkts'] + self.column_values['dpkts']
            try:
                self.column_values['sbytes'] = float(line[9])
            except (IndexError, ValueError):
                self.column_values['sbytes'] = 0
            try:
                self.column_values['dbytes'] = float(line[10])
            except (IndexError, ValueError):
                self.column_values['dbytes'] = 0
            self.column_values['bytes'] = self.column_values['sbytes'] + self.column_values['dbytes']
            try:
                self.column_values['state_hist'] = line[15]
            except IndexError:
                self.column_values['state_hist'] = self.column_values['state']
            # We do not know the indexes of MACs.
            self.column_values['smac'] = ''
            self.column_values['dmac'] = ''
        elif 'dns' in new_line['type']:
            self.column_values['type'] = 'dns'
            try:
                self.column_values['query'] = line[9]
            except IndexError:
                self.column_values['query'] = ''
            try:
                self.column_values['qclass_name'] = line[11]
            except IndexError:
                self.column_values['qclass_name'] = ''
            try:
                self.column_values['qtype_name'] = line[13]
            except IndexError:
                self.column_values['qtype_name'] = ''
            try:
                self.column_values['rcode_name'] = line[15]
            except IndexError:
                self.column_values['rcode_name'] = ''
            try:
                self.column_values['answers'] = line[21]
            except IndexError:
                self.column_values['answers'] = ''
            try:
                self.column_values['TTLs'] = line[22]
            except IndexError:
                self.column_values['TTLs'] = ''
        elif 'http' in new_line['type']:
            self.column_values['type'] = 'http'
            try:
                self.column_values['method'] = line[7]
            except IndexError:
                self.column_values['method'] = ''
            try:
                self.column_values['host'] = line[8]
            except IndexError:
                self.column_values['host'] = ''
            try:
                self.column_values['uri'] = line[9]
            except IndexError:
                self.column_values['uri'] = ''
            try:
                self.column_values['httpversion'] = line[11]
            except IndexError:
                self.column_values['httpversion'] = ''
            try:
                self.column_values['user_agent'] = line[12]
            except IndexError:
                self.column_values['user_agent'] = ''
            try:
                self.column_values['request_body_len'] = line[13]
            except IndexError:
                self.column_values['request_body_len'] = 0
            try:
                self.column_values['response_body_len'] = line[14]
            except IndexError:
                self.column_values['response_body_len'] = 0
            try:
                self.column_values['status_code'] = line[15]
            except IndexError:
                self.column_values['status_code'] = ''
            try:
                self.column_values['status_msg'] = line[16]
            except IndexError:
                self.column_values['status_msg'] = ''
            try:
                self.column_values['resp_mime_types'] = line[28]
            except IndexError:
                self.column_values['resp_mime_types'] = ''
            try:
                self.column_values['resp_fuids'] = line[26]
            except IndexError:
                self.column_values['resp_fuids'] = ''
        elif 'ssl' in new_line['type']:
            self.column_values['type'] = 'ssl'
            try:
                self.column_values['sslversion'] = line[6]
            except IndexError:
                self.column_values['sslversion'] = ''
            try:
                self.column_values['cipher'] = line[7]
            except IndexError:
                self.column_values['cipher'] = ''
            try:
                self.column_values['resumed'] = line[10]
            except IndexError:
                self.column_values['resumed'] = ''
            try:
                self.column_values['established'] = line[13]
            except IndexError:
                self.column_values['established'] = ''
            try:
                self.column_values['cert_chain_fuids'] = line[14]
            except IndexError:
                self.column_values['cert_chain_fuids'] = ''
            try:
                self.column_values['client_cert_chain_fuids'] = line[15]
            except IndexError:
                self.column_values['client_cert_chain_fuids'] = ''
            try:
                self.column_values['subject'] = line[16]
            except IndexError:
                self.column_values['subject'] = ''
            try:
                self.column_values['issuer'] = line[17]
            except IndexError:
                self.column_values['issuer'] = ''
            self.column_values['validation_status'] = ''
            try:
                self.column_values['curve'] = line[8]
            except IndexError:
                self.column_values['curve'] = ''
            try:
                self.column_values['server_name'] = line[9]
            except IndexError:
                self.column_values['server_name'] = ''
        elif 'ssh' in new_line['type']:
            self.column_values['type'] = 'ssh'
            try:
                self.column_values['version'] = line[6]
            except IndexError:
                self.column_values['version'] = ''
            # Zeek can put in column 7 the auth success if it has one
            # or the auth attempts only. However if the auth
            # success is there, the auth attempts are too.
            if 'success' in line[7]:
                try:
                    self.column_values['auth_success'] = line[7]
                except IndexError:
                    self.column_values['auth_success'] = ''
                try:
                    self.column_values['auth_attempts'] = line[8]
                except IndexError:
                    self.column_values['auth_attempts'] = ''
                try:
                    self.column_values['client'] = line[10]
                except IndexError:
                    self.column_values['client'] = ''
                try:
                    self.column_values['server'] = line[11]
                except IndexError:
                    self.column_values['server'] = ''
                try:
                    self.column_values['cipher_alg'] = line[12]
                except IndexError:
                    self.column_values['cipher_alg'] = ''
                try:
                    self.column_values['mac_alg'] = line[13]
                except IndexError:
                    self.column_values['mac_alg'] = ''
                try:
                    self.column_values['compression_alg'] = line[14]
                except IndexError:
                    self.column_values['compression_alg'] = ''
                try:
                    self.column_values['kex_alg'] = line[15]
                except IndexError:
                    self.column_values['kex_alg'] = ''
                try:
                    self.column_values['host_key_alg'] = line[16]
                except IndexError:
                    self.column_values['host_key_alg'] = ''
                try:
                    self.column_values['host_key'] = line[17]
                except IndexError:
                    self.column_values['host_key'] = ''
            else:
                self.column_values['auth_success'] = ''
                try:
                    self.column_values['auth_attempts'] = line[7]
                except IndexError:
                    self.column_values['auth_attempts'] = ''
                try:
                    self.column_values['client'] = line[9]
                except IndexError:
                    self.column_values['client'] = ''
                try:
                    self.column_values['server'] = line[10]
                except IndexError:
                    self.column_values['server'] = ''
                try:
                    self.column_values['cipher_alg'] = line[11]
                except IndexError:
                    self.column_values['cipher_alg'] = ''
                try:
                    self.column_values['mac_alg'] = line[12]
                except IndexError:
                    self.column_values['mac_alg'] = ''
                try:
                    self.column_values['compression_alg'] = line[13]
                except IndexError:
                    self.column_values['compression_alg'] = ''
                try:
                    self.column_values['kex_alg'] = line[14]
                except IndexError:
                    self.column_values['kex_alg'] = ''
                try:
                    self.column_values['host_key_alg'] = line[15]
                except IndexError:
                    self.column_values['host_key_alg'] = ''
                try:
                    self.column_values['host_key'] = line[16]
                except IndexError:
                    self.column_values['host_key'] = ''
        elif 'irc' in new_line['type']:
            self.column_values['type'] = 'irc'
        elif 'long' in new_line['type']:
            self.column_values['type'] = 'long'
        elif 'dhcp' in new_line['type']:
            self.column_values['type'] = 'dhcp'
        elif 'dce_rpc' in new_line['type']:
            self.column_values['type'] = 'dce_rpc'
        elif 'dnp3' in new_line['type']:
            self.column_values['type'] = 'dnp3'
        elif 'ftp' in new_line['type']:
            self.column_values['type'] = 'ftp'
        elif 'kerberos' in new_line['type']:
            self.column_values['type'] = 'kerberos'
        elif 'mysql' in new_line['type']:
            self.column_values['type'] = 'mysql'
        elif 'modbus' in new_line['type']:
            self.column_values['type'] = 'modbus'
        elif 'ntlm' in new_line['type']:
            self.column_values['type'] = 'ntlm'
        elif 'rdp' in new_line['type']:
            self.column_values['type'] = 'rdp'
        elif 'sip' in new_line['type']:
            self.column_values['type'] = 'sip'
        elif 'smb_cmd' in new_line['type']:
            self.column_values['type'] = 'smb_cmd'
        elif 'smb_files' in new_line['type']:
            self.column_values['type'] = 'smb_files'
        elif 'smb_mapping' in new_line['type']:
            self.column_values['type'] = 'smb_mapping'
        elif 'smtp' in new_line['type']:
            self.column_values['type'] = 'smtp'
        elif 'socks' in new_line['type']:
            self.column_values['type'] = 'socks'
        elif 'syslog' in new_line['type']:
            self.column_values['type'] = 'syslog'
        elif 'tunnel' in new_line['type']:
            self.column_values['type'] = 'tunnel'

    def process_zeek_input(self, new_line):
        """
        Process the line and extract columns for zeek
        Its a dictionary
        """
        line = new_line['data']
        file_type = new_line['type']
        # Generic fields in Zeek
        self.column_values = {'type': ''}
        try:
            self.column_values['starttime'] = self.get_time(line['ts'])
        except KeyError:
            self.column_values['starttime'] = ''
        try:
            self.column_values['uid'] = line['uid']
        except KeyError:
            self.column_values['uid'] = False
        try:
            self.column_values['saddr'] = line['id.orig_h']
        except KeyError:
            self.column_values['saddr'] = ''
        try:
            self.column_values['daddr'] = line['id.resp_h']
        except KeyError:
            self.column_values['daddr'] = ''

        if 'conn' in file_type:
            # {'ts': 1538080852.403669, 'uid': 'Cewh6D2USNVtfcLxZe', 'id.orig_h': '192.168.2.12', 'id.orig_p': 56343, 'id.resp_h': '192.168.2.1', 'id.resp_p': 53, 'proto': 'udp', 'service': 'dns', 'duration': 0.008364, 'orig_bytes': 30, 'resp_bytes': 94, 'conn_state': 'SF', 'missed_bytes': 0, 'history': 'Dd', 'orig_pkts': 1, 'orig_ip_bytes': 58, 'resp_pkts': 1, 'resp_ip_bytes': 122, 'orig_l2_addr': 'b8:27:eb:6a:47:b8', 'resp_l2_addr': 'a6:d1:8c:1f:ce:64', 'type': './zeek_files/conn'}
            self.column_values['type'] = 'conn'
            try:
                self.column_values['dur'] = float(line['duration'])
            except KeyError:
                self.column_values['dur'] = 0
            self.column_values['endtime'] = self.column_values['starttime'] + timedelta(seconds=self.column_values['dur'])
            self.column_values['proto'] = line['proto']
            try:
                self.column_values['appproto'] = line['service']
            except KeyError:
                # no service recognized
                self.column_values['appproto'] = ''
            try:
                self.column_values['sport'] = line['id.orig_p']
            except KeyError:
                self.column_values['sport'] = ''
            self.column_values['dir'] = '->'
            try:
                self.column_values['dport'] = line['id.resp_p']
            except KeyError:
                self.column_values['dport'] = ''
            try:
                self.column_values['state'] = line['conn_state']
            except KeyError:
                self.column_values['state'] = ''
            try:
                self.column_values['spkts'] = line['orig_pkts']
            except KeyError:
                self.column_values['spkts'] = 0
            try:
                self.column_values['dpkts'] = line['resp_pkts']
            except KeyError:
                self.column_values['dpkts'] = 0
            self.column_values['pkts'] = self.column_values['spkts'] + self.column_values['dpkts']
            try:
                self.column_values['sbytes'] = line['orig_bytes']
            except KeyError:
                self.column_values['sbytes'] = 0
            try:
                self.column_values['dbytes'] = line['resp_bytes']
            except KeyError:
                self.column_values['dbytes'] = 0
            self.column_values['bytes'] = self.column_values['sbytes'] + self.column_values['dbytes']
            try:
                self.column_values['state_hist'] = line['history']
            except KeyError:
                self.column_values['state_hist'] = self.column_values['state']
            try:
                self.column_values['smac'] = line['orig_l2_addr']
            except KeyError:
                self.column_values['smac'] = ''
            try:
                self.column_values['dmac'] = line['resp_l2_addr']
            except KeyError:
                self.column_values['dmac'] = ''
        elif 'dns' in file_type:
            #{"ts":1538080852.403669,"uid":"CtahLT38vq7vKJVBC3","id.orig_h":"192.168.2.12","id.orig_p":56343,"id.resp_h":"192.168.2.1","id.resp_p":53,"proto":"udp","trans_id":2,"rtt":0.008364,"query":"pool.ntp.org","qclass":1,"qclass_name":"C_INTERNET","qtype":1,"qtype_name":"A","rcode":0,"rcode_name":"NOERROR","AA":false,"TC":false,"RD":true,"RA":true,"Z":0,"answers":["185.117.82.70","212.237.100.250","213.251.52.107","183.177.72.201"],"TTLs":[42.0,42.0,42.0,42.0],"rejected":false}
            self.column_values['type'] = 'dns'
            try:
                self.column_values['query'] = line['query']
            except KeyError:
                self.column_values['query'] = ''
            try:
                self.column_values['qclass_name'] = line['qclass_name']
            except KeyError:
                self.column_values['qclass_name'] = ''
            try:
                self.column_values['qtype_name'] = line['qtype_name']
            except KeyError:
                self.column_values['qtype_name'] = ''
            try:
                self.column_values['rcode_name'] = line['rcode_name']
            except KeyError:
                self.column_values['rcode_name'] = ''
            try:
                self.column_values['answers'] = line['answers']
            except KeyError:
                self.column_values['answers'] = ''
            try:
                self.column_values['TTLs'] = line['TTLs']
            except KeyError:
                self.column_values['TTLs'] = ''
        elif 'http' in  file_type:
            # {"ts":158.957403,"uid":"CnNLbE2dyfy5KyqEhh","id.orig_h":"10.0.2.105","id.orig_p":49158,"id.resp_h":"64.182.208.181","id.resp_p":80,"trans_depth":1,"method":"GET","host":"icanhazip.com","uri":"/","version":"1.1","user_agent":"Mozilla/5.0 (Windows NT 6.1; WOW64) AppleWebKit/537.38 (KHTML, like Gecko) Chrome/45.0.2456.99 Safari/537.38","request_body_len":0,"response_body_len":13,"status_code":200,"status_msg":"OK","tags":[],"resp_fuids":["FwraVxIOACcjkaGi3"],"resp_mime_types":["text/plain"]}
            self.column_values['type'] = 'http'
            try:
                self.column_values['method'] = line['method']
            except KeyError:
                self.column_values['method'] = ''
            try:
                self.column_values['host'] = line['host']
            except KeyError:
                self.column_values['host'] = ''
            try:
                self.column_values['uri'] = line['uri']
            except KeyError:
                self.column_values['uri'] = ''
            try:
                self.column_values['httpversion'] = line['version']
            except KeyError:
                self.column_values['httpversion'] = ''
            try:
                self.column_values['user_agent'] = line['user_agent']
            except KeyError:
                self.column_values['user_agent'] = ''
            try:
                self.column_values['request_body_len'] = line['request_body_len']
            except KeyError:
                self.column_values['request_body_len'] = 0
            try:
                self.column_values['response_body_len'] = line['response_body_len']
            except KeyError:
                self.column_values['response_body_len'] = 0
            try:
                self.column_values['status_code'] = line['status_code']
            except KeyError:
                self.column_values['status_code'] = ''
            try:
                self.column_values['status_msg'] = line['status_msg']
            except KeyError:
                self.column_values['status_msg'] = ''
            try:
                self.column_values['resp_mime_types'] = line['resp_mime_types']
            except KeyError:
                self.column_values['resp_mime_types'] = ''
            try:
                self.column_values['resp_fuids'] = line['resp_fuids']
            except KeyError:
                self.column_values['resp_fuids'] = ''
        elif 'ssl' in file_type:
            # {"ts":12087.045499,"uid":"CdoFDp4iW79I5ZmsT7","id.orig_h":"10.0.2.105","id.orig_p":49704,"id.resp_h":"195.211.240.166","id.resp_p":443,"version":"SSLv3","cipher":"TLS_RSA_WITH_RC4_128_SHA","resumed":false,"established":true,"cert_chain_fuids":["FhGp1L3yZXuURiPqq7"],"client_cert_chain_fuids":[],"subject":"OU=DAHUATECH,O=DAHUA,L=HANGZHOU,ST=ZHEJIANG,C=CN,CN=192.168.1.108","issuer":"O=DahuaTech,L=HangZhou,ST=ZheJiang,C=CN,CN=Product Root CA","validation_status":"unable to get local issuer certificate"}
            # {"ts":1382354909.915615,"uid":"C7W6ZA4vI8FxJ9J0bh","id.orig_h":"147.32.83.53","id.orig_p":36567,"id.resp_h":"195.113.214.241","id.resp_p":443,"version":"TLSv12","cipher":"TLS_ECDHE_ECDSA_WITH_RC4_128_SHA","curve":"secp256r1","server_name":"id.google.com.ar","resumed":false,"established":true,"cert_chain_fuids":["FnomJz1vghKIOHtytf","FSvQff1KsaDkRtKXo4","Fif2PF48bytqq6xMDb"],"client_cert_chain_fuids":[],"subject":"CN=*.google.com,O=Google Inc,L=Mountain View,ST=California,C=US","issuer":"CN=Google Internet Authority G2,O=Google Inc,C=US","validation_status":"ok"}
            self.column_values['type'] = 'ssl'
            try:
                self.column_values['sslversion'] = line['version']
            except KeyError:
                self.column_values['sslversion'] = ''
            try:
                self.column_values['cipher'] = line['cipher']
            except KeyError:
                self.column_values['cipher'] = ''
            try:
                self.column_values['resumed'] = line['resumed']
            except KeyError:
                self.column_values['resumed'] = ''
            try:
                self.column_values['established'] = line['established']
            except KeyError:
                self.column_values['established'] = ''
            try:
                self.column_values['cert_chain_fuids'] = line['cert_chain_fuids']
            except KeyError:
                self.column_values['cert_chain_fuids'] = ''
            try:
                self.column_values['client_cert_chain_fuids'] = line['client_cert_chain_fuids']
            except KeyError:
                self.column_values['client_cert_chain_fuids'] = ''
            try:
                self.column_values['subject'] = line['subject']
            except KeyError:
                self.column_values['subject'] = ''
            try:
                self.column_values['issuer'] = line['issuer']
            except KeyError:
                self.column_values['issuer'] = ''
            try:
                self.column_values['validation_status'] = line['validation_status']
            except KeyError:
                self.column_values['validation_status'] = ''
            try:
                self.column_values['curve'] = line['curve']
            except KeyError:
                self.column_values['curve'] = ''
            try:
                self.column_values['server_name'] = line['server_name']
            except KeyError:
                self.column_values['server_name'] = ''
        elif 'ssh' in file_type:
            self.column_values['type'] = 'ssh'
            try:
                self.column_values['version'] = line['version']
            except KeyError:
                self.column_values['version'] = ''
            try:
                self.column_values['auth_success'] = line['auth_success']
            except KeyError:
                self.column_values['auth_success'] = ''
            try:
                self.column_values['auth_attempts'] = line['auth_attempts']
            except KeyError:
                self.column_values['auth_attempts'] = ''
            try:
                self.column_values['client'] = line['client']
            except KeyError:
                self.column_values['client'] = ''
            try:
                self.column_values['server'] = line['server']
            except KeyError:
                self.column_values['server'] = ''
            try:
                self.column_values['cipher_alg'] = line['cipher_alg']
            except KeyError:
                self.column_values['cipher_alg'] = ''
            try:
                self.column_values['mac_alg'] = line['mac_alg']
            except KeyError:
                self.column_values['mac_alg'] = ''
            try:
                self.column_values['compression_alg'] = line['compression_alg']
            except KeyError:
                self.column_values['compression_alg'] = ''
            try:
                self.column_values['kex_alg'] = line['kex_alg']
            except KeyError:
                self.column_values['kex_alg'] = ''
            try:
                self.column_values['host_key_alg'] = line['host_key_alg']
            except KeyError:
                self.column_values['host_key_alg'] = ''
            try:
                self.column_values['host_key'] = line['host_key']
            except KeyError:
                self.column_values['host_key'] = ''
        elif 'irc' in file_type:
            self.column_values['type'] = 'irc'
        elif 'long' in file_type:
            self.column_values['type'] = 'long'
        elif 'dhcp' in file_type:
            self.column_values['type'] = 'dhcp'
        elif 'dce_rpc' in file_type:
            self.column_values['type'] = 'dce_rpc'
        elif 'dnp3' in file_type:
            self.column_values['type'] = 'dnp3'
        elif 'ftp' in file_type:
            self.column_values['type'] = 'ftp'
        elif 'kerberos' in file_type:
            self.column_values['type'] = 'kerberos'
        elif 'mysql' in file_type:
            self.column_values['type'] = 'mysql'
        elif 'modbus' in file_type:
            self.column_values['type'] = 'modbus'
        elif 'ntlm' in file_type:
            self.column_values['type'] = 'ntlm'
        elif 'rdp' in file_type:
            self.column_values['type'] = 'rdp'
        elif 'sip' in file_type:
            self.column_values['type'] = 'sip'
        elif 'smb_cmd' in file_type:
            self.column_values['type'] = 'smb_cmd'
        elif 'smb_files' in file_type:
            self.column_values['type'] = 'smb_files'
        elif 'smb_mapping' in file_type:
            self.column_values['type'] = 'smb_mapping'
        elif 'smtp' in file_type:
            self.column_values['type'] = 'smtp'
        elif 'socks' in file_type:
            self.column_values['type'] = 'socks'
        elif 'syslog' in file_type:
            self.column_values['type'] = 'syslog'
        elif 'tunnel' in file_type:
            self.column_values['type'] = 'tunnel'

    def process_argus_input(self, new_line):
        """
        Process the line and extract columns for argus
        """
        line = new_line['data']
        self.column_values = {
            'starttime': False,
            'endtime': False,
            'dur': False,
            'proto': False,
            'appproto': False,
            'saddr': False,
            'sport': False,
            'dir': False,
            'daddr': False,
            'dport': False,
            'state': False,
            'pkts': False,
            'spkts': False,
            'dpkts': False,
            'bytes': False,
            'sbytes': False,
            'dbytes': False,
            'type': 'argus',
        }
        # Read the lines fast
        nline = line.strip().split(self.separator)
        try:
            self.column_values['starttime'] = self.get_time(nline[self.column_idx['starttime']])
        except KeyError:
            pass
        try:
            self.column_values['endtime'] = nline[self.column_idx['endtime']]
        except KeyError:
            pass
        try:
            self.column_values['dur'] = nline[self.column_idx['dur']]
        except KeyError:
            pass
        try:
            self.column_values['proto'] = nline[self.column_idx['proto']]
        except KeyError:
            pass
        try:
            self.column_values['appproto'] = nline[self.column_idx['appproto']]
        except KeyError:
            pass
        try:
            self.column_values['saddr'] = nline[self.column_idx['saddr']]
        except KeyError:
            pass
        try:
            self.column_values['sport'] = nline[self.column_idx['sport']]
        except KeyError:
            pass
        try:
            self.column_values['dir'] = nline[self.column_idx['dir']]
        except KeyError:
            pass
        try:
            self.column_values['daddr'] = nline[self.column_idx['daddr']]
        except KeyError:
            pass
        try:
            self.column_values['dport'] = nline[self.column_idx['dport']]
        except KeyError:
            pass
        try:
            self.column_values['state'] = nline[self.column_idx['state']]
        except KeyError:
            pass
        try:
            self.column_values['pkts'] = int(nline[self.column_idx['pkts']])
        except KeyError:
            pass
        try:
            self.column_values['spkts'] = int(nline[self.column_idx['spkts']])
        except KeyError:
            pass
        try:
            self.column_values['dpkts'] = int(nline[self.column_idx['dpkts']])
        except KeyError:
            pass
        try:
            self.column_values['bytes'] = int(nline[self.column_idx['bytes']])
        except KeyError:
            pass
        try:
            self.column_values['sbytes'] = int(nline[self.column_idx['sbytes']])
        except KeyError:
            pass
        try:
            self.column_values['dbytes'] = int(nline[self.column_idx['dbytes']])
        except KeyError:
            pass

    def process_nfdump_input(self, new_line):
        """
        Process the line and extract columns for nfdump
        """
        self.column_values = {
            'starttime': False,
            'endtime': False,
            'dur': False,
            'proto': False,
            'appproto': False,
            'saddr': False,
            'sport': False,
            'dir': False,
            'daddr': False,
            'dport': False,
            'state': False,
            'pkts': False,
            'spkts': False,
            'dpkts': False,
            'bytes': False,
            'sbytes': False,
            'dbytes': False,
            'type': 'nfdump',
        }
        # Read the lines fast
        line = new_line['data']
        nline = line.strip().split(self.separator)
        try:
            self.column_values['starttime'] = self.get_time(nline[0])
        except IndexError:
            pass
        try:
            self.column_values['endtime'] = self.get_time(nline[1])
        except IndexError:
            pass
        try:
            self.column_values['dur'] = nline[2]
        except IndexError:
            pass
        try:
            self.column_values['proto'] = nline[7]
        except IndexError:
            pass
        try:
            self.column_values['saddr'] = nline[3]
        except IndexError:
            pass
        try:
            self.column_values['sport'] = nline[5]
        except IndexError:
            pass
        try:
            # Direction: ingress=0, egress=1
            self.column_values['dir'] = nline[22]
        except IndexError:
            pass
        try:
            self.column_values['daddr'] = nline[4]
        except IndexError:
            pass
        try:
            self.column_values['dport'] = nline[6]
        except IndexError:
            pass
        try:
            self.column_values['state'] = nline[8]
        except IndexError:
            pass
        try:
            self.column_values['spkts'] = nline[11]
        except IndexError:
            pass
        try:
            self.column_values['dpkts'] = nline[13]
        except IndexError:
            pass
        try:
            self.column_values['pkts'] = self.column_values['spkts'] + self.column_values['dpkts']
        except IndexError:
            pass
        try:
            self.column_values['sbytes'] = nline[12]
        except IndexError:
            pass
        try:
            self.column_values['dbytes'] = nline[14]
        except IndexError:
            pass
        try:
            self.column_values['bytes'] = self.column_values['sbytes'] + self.column_values['dbytes']
        except IndexError:
            pass

    def process_suricata_input(self, line: str) -> None:
        """ Read suricata json input """
        line = json.loads(line)

        self.column_values: dict = {}
        try:
            self.column_values['starttime'] = self.get_time(line['timestamp'])
        # except (KeyError, ValueError):
        except ValueError:
            # Reason for catching ValueError:
            # "ValueError: time data '1900-01-00T00:00:08.511802+0000' does not match format '%Y-%m-%dT%H:%M:%S.%f%z'"
            # It means some flow do not have valid timestamp. It seems to me if suricata does not know the timestamp, it put
            # there this not valid time.
            self.column_values['starttime'] = False
        self.column_values['endtime'] = False
        self.column_values['dur'] = 0
        try:
            self.column_values['flow_id'] = line['flow_id']
        except KeyError:
            self.column_values['flow_id'] = False
        try:
            self.column_values['saddr'] = line['src_ip']
        except KeyError:
            self.column_values['saddr'] = False
        try:
            self.column_values['sport'] = line['src_port']
        except KeyError:
            self.column_values['sport'] = False
        try:
            self.column_values['daddr'] = line['dest_ip']
        except KeyError:
            self.column_values['daddr'] = False
        try:
            self.column_values['dport'] = line['dest_port']
        except KeyError:
            self.column_values['dport'] = False
        try:
            self.column_values['proto'] = line['proto']
        except KeyError:
            self.column_values['proto'] = False
        try:
            self.column_values['type'] = line['event_type']
        except KeyError:
            self.column_values['type'] = False
        self.column_values['dir'] = '->'
        try:
            self.column_values['appproto'] = line['app_proto']
        except KeyError:
            self.column_values['appproto'] = False

        if self.column_values['type']:
            """
            event_type: 
            -flow
            -tls
            -http
            -dns
            -alert
            -fileinfo
            -stats (only one line - it is conclusion of entire capture)
            """
            if self.column_values['type'] == 'flow':
                # A suricata line of flow type usually has 2 components.
                # 1. flow information
                # 2. tcp information
                if line.get('flow', None):
                    try:
                        # Define time again, because this is line of flow type and
                        # we do not want timestamp but start time.
                        self.column_values['starttime'] = self.get_time(line['flow']['start'])
                    except KeyError:
                        self.column_values['starttime'] = False
                    try:
                        self.column_values['endtime'] = self.get_time(line['flow']['end'])
                    except KeyError:
                        self.column_values['endtime'] = False

                    try:
                        self.column_values['dur'] = (
                            self.column_values['endtime'] - self.column_values['starttime']).total_seconds()
                    except (KeyError, TypeError):
                        self.column_values['dur'] = 0
                    try:
                        self.column_values['spkts'] = line['flow']['pkts_toserver']
                    except KeyError:
                        self.column_values['spkts'] = 0
                    try:
                        self.column_values['dpkts'] = line['flow']['pkts_toclient']
                    except KeyError:
                        self.column_values['dpkts'] = 0

                    self.column_values['pkts'] = self.column_values['dpkts'] + self.column_values['spkts']

                    try:
                        self.column_values['sbytes'] = line['flow']['bytes_toserver']
                    except KeyError:
                        self.column_values['sbytes'] = 0

                    try:
                        self.column_values['dbytes'] = line['flow']['bytes_toclient']
                    except KeyError:
                        self.column_values['dbytes'] = 0

                    self.column_values['bytes'] = self.column_values['dbytes'] + self.column_values['sbytes']

                    try:
                        """
                        There are different states in which a flow can be. 
                        Suricata distinguishes three flow-states for TCP and two for UDP. For TCP, 
                        these are: New, Established and Closed,for UDP only new and established.
                        For each of these states Suricata can employ different timeouts. 
                        """
                        self.column_values['state'] = line['flow']['state']
                    except KeyError:
                        self.column_values['state'] = ''
            elif self.column_values['type'] == 'http':
                if line.get('http', None):
                    try:
                        self.column_values['method'] = line['http']['http_method']
                    except KeyError:
                        self.column_values['method'] = ''
                    try:
                        self.column_values['host'] = line['http']['hostname']
                    except KeyError:
                        self.column_values['host'] = ''
                    try:
                        self.column_values['uri'] = line['http']['url']
                    except KeyError:
                        self.column_values['uri'] = ''
                    try:
                        self.column_values['user_agent'] = line['http']['http_user_agent']
                    except KeyError:
                        self.column_values['user_agent'] = ''
                    try:
                        self.column_values['status_code'] = line['http']['status']
                    except KeyError:
                        self.column_values['status_code'] = ''
                    try:
                        self.column_values['httpversion'] = line['http']['protocol']
                    except KeyError:
                        self.column_values['httpversion'] = ''
                    try:
                        self.column_values['response_body_len'] = line['http']['length']
                    except KeyError:
                        self.column_values['response_body_len'] = 0
                    try:
                        self.column_values['request_body_len'] = line['http']['request_body_len']
                    except KeyError:
                        self.column_values['request_body_len'] = 0
                    self.column_values['status_msg'] = ''
                    self.column_values['resp_mime_types'] = ''
                    self.column_values['resp_fuids'] = ''

            elif self.column_values['type'] == 'dns':
                if line.get('dns', None):
                    try:
                        self.column_values['query'] = line['dns']['rdata']
                    except KeyError:
                        self.column_values['query'] = ''
                    try:
                        self.column_values['TTLs'] = line['dns']['ttl']
                    except KeyError:
                        self.column_values['TTLs'] = ''

                    try:
                        self.column_values['qtype_name'] = line['dns']['rrtype']
                    except KeyError:
                        self.column_values['qtype_name'] = ''

                    # can not find in eve.json:
                    self.column_values['qclass_name'] = ''
                    self.column_values['rcode_name'] = ''
                    self.column_values['answers'] = ''


            elif self.column_values['type'] == 'tls':
                if line.get('tls', None):
                    try:
                        self.column_values['sslversion'] = line['tls']['version']
                    except KeyError:
                        self.column_values['sslversion'] = ''
                    try:
                        self.column_values['subject'] = line['tls']['subject']
                    except KeyError:
                        self.column_values['subject'] = ''
                    try:
                        self.column_values['issuer'] = line['tls']['issuerdn']
                    except KeyError:
                        self.column_values['issuer'] = ''
                    try:
                        self.column_values['server_name'] = line['tls']['sni']
                    except KeyError:
                        self.column_values['server_name'] = ''

                    try:
                        self.column_values['notbefore'] = datetime.strptime((line['tls']['notbefore']), '%Y-%m-%dT%H:%M:%S')
                    except KeyError:
                        self.column_values['notbefore'] = ''

                    try:
                        self.column_values['notafter'] = datetime.strptime((line['tls']['notafter']), '%Y-%m-%dT%H:%M:%S')
                    except KeyError:
                        self.column_values['notafter'] = ''
            elif self.column_values['type'] == 'alert':
                if line.get('alert', None):
                    try:
                        self.column_values['signature'] = line['alert']['signature']
                    except KeyError:
                        self.column_values['signature'] = ''
                    try:
                        self.column_values['category'] = line['alert']['category']
                    except KeyError:
                        self.column_values['category'] = ''
                    try:
                        self.column_values['severity'] = line['alert']['severity']
                    except KeyError:
                        self.column_values['severity'] = ''
            elif self.column_values['type'] == 'fileinfo':
                try:
                    self.column_values['filesize'] = line['fileinfo']['size']
                except KeyError:
                    self.column_values['filesize'] = ''

    def add_flow_to_profile(self):
        """
        This is the main function that takes the columns of a flow and does all the magic to convert it into a working data in our system.
        It includes checking if the profile exists and how to put the flow correctly.
        It interprets each colum
        A flow has two IP addresses, so treat both of them correctly.
        """
        try:
            # Define which type of flows we are going to preocess
            if not self.column_values:
                return True
            elif (
                'ssh' not in self.column_values['type']
                and 'ssl' not in self.column_values['type']
                and 'http' not in self.column_values['type']
                and 'dns' not in self.column_values['type']
                and 'conn' not in self.column_values['type']
                and 'flow' not in self.column_values['type']
                and 'argus' not in self.column_values['type']
                and 'nfdump' not in self.column_values['type']
            ):
                return True
            elif self.column_values['starttime'] is None:
                # There is suricata issue with invalid timestamp for examaple: "1900-01-00T00:00:08.511802+0000"
                return True

            try:
                # seconds.
                starttime = self.column_values['starttime'].timestamp()
            except ValueError:
                # date
                try:
                    # This file format is very extended, but we should consider more options. Maybe we should detect the time format.
                    # Some times if there is no microseconds, the datatime object just give us '2018-12-18 14:00:00' instead of '2018-12-18 14:00:00.000000' so the format fails
                    try:
                        date_time = datetime.strptime(str(self.column_values['starttime']), '%Y-%m-%d %H:%M:%S.%f')
                    except ValueError:
                        date_time = datetime.strptime(str(self.column_values['starttime']) + '.000000', '%Y-%m-%d %H:%M:%S.%f')
                    starttime = date_time.timestamp()
                except ValueError as e:
                    self.print("We can not recognize time format.", 0, 1)
                    self.print("{}".format((type(e))), 0, 1)

            # This uid check is for when we read things that are not zeek
            try:
                uid = self.column_values['uid']
            except KeyError:
                # In the case of other tools that are not Zeek, there is no UID. So we generate a new one here
                # Zeeks uses human-readable strings in Base62 format, from 112 bits usually. We do base64 with some bits just because we need a fast unique way
                uid = base64.b64encode(binascii.b2a_hex(os.urandom(9))).decode('utf-8')

            flow_type = self.column_values['type']
            saddr = self.column_values['saddr']
            daddr = self.column_values['daddr']
            profileid = f'profile{self.id_separator}{str(saddr)}'

            if 'flow' in flow_type or 'conn' in flow_type or 'argus' in flow_type or 'nfdump' in flow_type:
                dur = self.column_values['dur']
                sport = self.column_values['sport']
                dport = self.column_values['dport']
                sport = self.column_values['sport']
                proto = self.column_values['proto']
                state = self.column_values['state']
                pkts = self.column_values['pkts']
                allbytes = self.column_values['bytes']
                spkts = self.column_values['spkts']
                sbytes = self.column_values['sbytes']
                endtime = self.column_values['endtime']
                appproto = self.column_values['appproto']
                direction = self.column_values['dir']
                dpkts = self.column_values['dpkts']
                dbytes = self.column_values['dbytes']

            elif 'dns' in flow_type:
                query = self.column_values['query']
                qclass_name = self.column_values['qclass_name']
                qtype_name = self.column_values['qtype_name']
                rcode_name = self.column_values['rcode_name']
                answers = self.column_values['answers']
                ttls = self.column_values['TTLs']

            # Create the objects of IPs
            try:
                saddr_as_obj = ipaddress.IPv4Address(saddr)
                daddr_as_obj = ipaddress.IPv4Address(daddr)
                # Is ipv4
            except ipaddress.AddressValueError:
                # Is it ipv6?
                try:
                    saddr_as_obj = ipaddress.IPv6Address(saddr)
                    daddr_as_obj = ipaddress.IPv6Address(daddr)
                except ipaddress.AddressValueError:
                    # Its a mac
                    return False

            ##############
            # 4th Define help functions for storing data
            def store_features_going_out(profileid, twid, starttime):
                """
                This is an internal function in the add_flow_to_profile function for adding the features going out of the profile
                """
                role = 'Client'
                # self.print(f'Storing features going out for profile {profileid} and tw {twid}')
                if 'flow' in flow_type or 'conn' in flow_type or 'argus' in flow_type or 'nfdump' in flow_type:
                    # Tuple
                    tupleid = f'{str(daddr_as_obj)}:{str(dport)}:{proto}'
                    # Compute the symbol for this flow, for this TW, for this profile. The symbol is based on the 'letters' of the original Startosphere ips tool
                    symbol = self.compute_symbol(profileid, twid, tupleid, starttime, dur, allbytes, tuple_key='OutTuples')
                    # Change symbol for its internal data. Symbol is a tuple and is confusing if we ever change the API
                    # Add the out tuple
                    __database__.add_tuple(profileid, twid, tupleid, symbol, role, starttime)
                    # Add the dstip
                    __database__.add_ips(profileid, twid, daddr_as_obj, self.column_values, role)
                    # Add the dstport
                    port_type = 'Dst'
                    __database__.add_port(profileid, twid, daddr_as_obj, self.column_values, role, port_type)
                    # Add the srcport
                    port_type = 'Src'
                    __database__.add_port(profileid, twid, daddr_as_obj, self.column_values, role, port_type)
                    # Add the flow with all the fields interpreted
                    __database__.add_flow(profileid=profileid, twid=twid, stime=starttime, dur=dur, saddr=str(saddr_as_obj), sport=sport, daddr=str(daddr_as_obj), dport=dport, proto=proto, state=state, pkts=pkts, allbytes=allbytes, spkts=spkts, sbytes=sbytes, appproto=appproto, uid=uid, label=self.label)
                elif 'dns' in flow_type:
                    __database__.add_out_dns(profileid, twid, flow_type, uid, query, qclass_name, qtype_name, rcode_name, answers, ttls)
                    # Add DNS resolution if there are answers for the query
                    if answers:
                        __database__.set_dns_resolution(query, answers)
                elif flow_type == 'http':
                    __database__.add_out_http(profileid, twid, flow_type, uid, self.column_values['method'], self.column_values['host'], self.column_values['uri'], self.column_values['httpversion'], self.column_values['user_agent'], self.column_values['request_body_len'], self.column_values['response_body_len'], self.column_values['status_code'], self.column_values['status_msg'], self.column_values['resp_mime_types'], self.column_values['resp_fuids'])
                elif flow_type == 'ssl':
                    __database__.add_out_ssl(profileid, twid, daddr_as_obj, flow_type, uid, self.column_values['sslversion'], self.column_values['cipher'], self.column_values['resumed'], self.column_values['established'], self.column_values['cert_chain_fuids'], self.column_values['client_cert_chain_fuids'], self.column_values['subject'], self.column_values['issuer'], self.column_values['validation_status'], self.column_values['curve'], self.column_values['server_name'])
                elif flow_type == 'ssh':
                    __database__.add_out_ssh(profileid, twid, flow_type, uid, self.column_values['version'], self.column_values['auth_attempts'], self.column_values['auth_success'], self.column_values['client'], self.column_values['server'], self.column_values['cipher_alg'], self.column_values['mac_alg'], self.column_values['compression_alg'], self.column_values['kex_alg'], self.column_values['host_key_alg'], self.column_values['host_key'])

            def store_features_going_in(profileid, twid, starttime):
                """
                This is an internal function in the add_flow_to_profile function for adding the features going in of the profile
                """
                role = 'Server'
                # self.print(f'Storing features going in for profile {profileid} and tw {twid}')
                if 'flow' in flow_type or 'conn' in flow_type or 'argus' in flow_type or 'nfdump' in flow_type:
                    # Tuple. We use the src ip, but the dst port still!
                    tupleid = f'{str(saddr_as_obj)}:{str(dport)}:{proto}'
                    # Compute symbols.
                    symbol = self.compute_symbol(profileid, twid, tupleid, starttime, dur, allbytes, tuple_key='InTuples')
                    # Add the src tuple
                    __database__.add_tuple(profileid, twid, tupleid, symbol, role, starttime)
                    # Add the srcip
                    __database__.add_ips(profileid, twid, saddr_as_obj, self.column_values, role)
                    # Add the dstport
                    port_type = 'Dst'
                    __database__.add_port(profileid, twid, daddr_as_obj, self.column_values, role, port_type)
                    # Add the srcport
                    port_type = 'Src'
                    __database__.add_port(profileid, twid, daddr_as_obj, self.column_values, role, port_type)
                    # Add the flow with all the fields interpreted
                    __database__.add_flow(profileid=profileid, twid=twid, stime=starttime, dur=dur, saddr=str(saddr_as_obj), sport=sport, daddr=str(daddr_as_obj), dport=dport, proto=proto, state=state, pkts=pkts, allbytes=allbytes, spkts=spkts, sbytes=sbytes, appproto=appproto, uid=uid, label=self.label)
                                # No dns check going in. Probably ok.


            def get_rev_profile(starttime, daddr_as_obj):
                # Compute the rev_profileid
                rev_profileid = __database__.getProfileIdFromIP(daddr_as_obj)
                if not rev_profileid:
                    self.print("The dstip profile was not here... create", 0, 7)
                    # Create a reverse profileid for managing the data going to the dstip.
                    rev_profileid = f'profile{self.id_separator}{str(daddr_as_obj)}'
                    __database__.addProfile(rev_profileid, starttime, self.width)
                    # Try again
                    rev_profileid = __database__.getProfileIdFromIP(daddr_as_obj)
                                # For the profile to the dstip, find the id in the database of the tw where the flow belongs.
                rev_twid = self.get_timewindow(starttime, rev_profileid)
                return rev_profileid, rev_twid

            ##########################################
            # 5th. Store the data according to the paremeters
            # Now that we have the profileid and twid, add the data from the flow in this tw for this profile
            self.print("Storing data in the profile: {}".format(profileid), 0, 7)

            # For this 'forward' profile, find the id in the database of the tw where the flow belongs.
            twid = self.get_timewindow(starttime, profileid)

            if self.home_net:
                # Home. Create profiles for home IPs only
                if self.analysis_direction == 'out':
                    # Home and only out. Check if the src IP is in the home. If yes, store only out
                    if saddr_as_obj in self.home_net:
                        __database__.addProfile(profileid, starttime, self.width)
                        store_features_going_out(profileid, twid, starttime)
                elif self.analysis_direction == 'all':
                    # Home and all. Check if src IP or dst IP are in home. Store all
                    if saddr_as_obj in self.home_net:
                        __database__.addProfile(profileid, starttime, self.width)
                        store_features_going_out(profileid, twid, starttime)
                    if daddr_as_obj in self.home_net:
                        rev_profileid, rev_twid = get_rev_profile(starttime, daddr_as_obj)
                        store_features_going_in(rev_profileid, rev_twid, starttime)
            elif not self.home_net:
                # No home. Create profiles for everybody
                if self.analysis_direction == 'out':
                    # No home. Only store out
                    __database__.addProfile(profileid, starttime, self.width)
                    store_features_going_out(profileid, twid, starttime)
                elif self.analysis_direction == 'all':
                    # No home. Store all
                    __database__.addProfile(profileid, starttime, self.width)
                    rev_profileid, rev_twid = get_rev_profile(starttime, daddr_as_obj)
                    store_features_going_out(profileid, twid, starttime)
                    store_features_going_in(rev_profileid, rev_twid, starttime)

            """
            # 2nd. Check home network
            # Check if the ip received (src_ip) is part of our home network. We only crate profiles for our home network
            if self.home_net and saddr_as_obj in self.home_net:
                # Its in our Home network

                # The steps for adding a flow in a profile should be
                # 1. Add the profile to the DB. If it already exists, nothing happens. So now profileid is the id of the profile to work with.
                # The width is unique for all the timewindow in this profile.
                # Also we only need to pass the width for registration in the DB. Nothing operational
                __database__.addProfile(profileid, starttime, self.width)

                # 3. For this profile, find the id in the database of the tw where the flow belongs.
                twid = self.get_timewindow(starttime, profileid)

            elif self.home_net and saddr_as_obj not in self.home_net:
                # The src ip is not in our home net

                # Check that the dst IP is in our home net. Like the flow is 'going' to it.
                if daddr_as_obj in self.home_net:
                    self.print("Flow with dstip in homenet: srcip {}, dstip {}".format(saddr_as_obj, daddr_as_obj), 0, 7)
                    # The dst ip is in the home net. So register this as going to it
                    # 1. Get the profile of the dst ip.
                    rev_profileid = __database__.getProfileIdFromIP(daddr_as_obj)
                    if not rev_profileid:
                        # We do not have yet the profile of the dst ip that is in our home net
                        self.print("The dstip profile was not here... create", 0, 7)
                        # Create a reverse profileid for managing the data going to the dstip.
                        # With the rev_profileid we can now work with data in relation to the dst ip
                        rev_profileid = 'profile' + self.id_separator + str(daddr_as_obj)
                        __database__.addProfile(rev_profileid, starttime, self.width)
                        # Try again
                        rev_profileid = __database__.getProfileIdFromIP(daddr_as_obj)
                        # For the profile to the dstip, find the id in the database of the tw where the flow belongs.
                        rev_twid = self.get_timewindow(starttime, rev_profileid)
                        if not rev_profileid:
                            # Too many errors. We should not be here
                            return False
                    self.print("Profile for dstip {} : {}".format(daddr_as_obj, profileid), 0, 7)
                    # 2. For this profile, find the id in the databse of the tw where the flow belongs.
                    rev_twid = self.get_timewindow(starttime, profileid)
                elif daddr_as_obj not in self.home_net:
                    # The dst ip is also not part of our home net. So ignore completely
                    return False
            elif not self.home_net:
                # We don't have a home net, so create profiles for everyone. 

                # Add the profile for the srcip to the DB. If it already exists, nothing happens. So now profileid is the id of the profile to work with.
                __database__.addProfile(profileid, starttime, self.width)
                # Add the profile for the dstip to the DB. If it already exists, nothing happens. So now rev_profileid is the id of the profile to work with. 
                rev_profileid = 'profile' + self.id_separator + str(daddr_as_obj)
                __database__.addProfile(rev_profileid, starttime, self.width)

                # For the profile from the srcip , find the id in the database of the tw where the flow belongs.
                twid = self.get_timewindow(starttime, profileid)
                # For the profile to the dstip, find the id in the database of the tw where the flow belongs.
                rev_twid = self.get_timewindow(starttime, rev_profileid)

            # In which analysis mode are we?
            # Mode 'out'
            if self.analysis_direction == 'out':
                # Only take care of the stuff going out. Here we don't keep track of the stuff going in
                # If we have a home net and the flow comes from it, or if we don't have a home net and we are in out out.
                if (self.home_net and saddr_as_obj in self.home_net) or not self.home_net:
                    store_features_going_out(profileid, twid, starttime)

            # Mode 'all'
            elif self.analysis_direction == 'all':
                # Take care of both the stuff going out and in. In case the profile is for the srcip and for the dstip
                if not self.home_net:
                    # If we don't have a home net, just try to store everything coming OUT and IN to the IP
                    # Out features
                    store_features_going_out(profileid, twid, starttime)
                    # IN features
                    store_features_going_in(rev_profileid, rev_twid, starttime)
                else:
                    # The flow is going TO homenet or FROM homenet or BOTH together.
                    # If we have a home net and the flow comes from it. Only the features going out of the IP
                    if saddr_as_obj in self.home_net:
                        store_features_going_out(profileid, twid, starttime)
                    # If we have a home net and the flow comes to it. Only the features going in of the IP
                    elif daddr_as_obj in self.home_net:
                        # The dstip was in the homenet. Add the src info to the dst profile
                        self.print('Features going in')
                        store_features_going_in(rev_profileid, rev_twid, starttime)
                    # If the flow is going from homenet to homenet.
                    elif daddr_as_obj in self.home_net and saddr_as_obj in self.home_net:
                        store_features_going_out(profileid, twid, starttime)
                        self.print('Features going in')
                        store_features_going_in(rev_profileid, rev_twid, starttime)
            """
        except Exception as inst:
            # For some reason we can not use the output queue here.. check
            self.print("Error in add_flow_to_profile profilerProcess. {}".format(traceback.format_exc()), 0, 1)
            self.print("{}".format((type(inst))), 0, 1)
            self.print("{}".format(inst), 0, 1)

    def compute_symbol(self, profileid, twid, tupleid, current_time, current_duration, current_size, tuple_key: str):
        """
        This function computes the new symbol for the tuple according to the
        original stratosphere ips model of letters
        Here we do not apply any detection model, we just create the letters
        as one more feature current_time is the starttime of the flow
        """
        try:
            current_duration = float(current_duration)
            current_size = int(current_size)
            now_ts = float(current_time)
            self.print("Starting compute symbol. Profileid: {}, Tupleid {}, time:{} ({}), dur:{}, size:{}".format(profileid, tupleid, current_time, type(current_time), current_duration, current_size), 0, 8)
            # Variables for computing the symbol of each tuple
            T2 = False
            TD = False
            # Thresholds learnt from Stratosphere ips first version
            # Timeout time, after 1hs
            tto = timedelta(seconds=3600)
            tt1 = float(1.05)
            tt2 = float(1.3)
            tt3 = float(5)
            td1 = float(0.1)
            td2 = float(10)
            ts1 = float(250)
            ts2 = float(1100)
            letter = ''
            symbol = ''
            timechar = ''

            # Get the time of the last flow in this tuple, and the last last
            # Implicitely this is converting what we stored as 'now' into 'last_ts' and what we stored as 'last_ts' as 'last_last_ts'
            (last_last_ts, last_ts) = __database__.getT2ForProfileTW(profileid, twid, tupleid, tuple_key)
            # self.print(f'Profileid: {profileid}. Data extracted from DB. last_ts: {last_ts}, last_last_ts: {last_last_ts}', 0, 5)

            def compute_periodicity(now_ts: float, last_ts: float, last_last_ts: float):
                """ Function to compute the periodicity """
                zeros = ''
                if last_last_ts is False or last_ts is False:
                    TD = -1
                    T1 = None
                    T2 = None
                else:
                    # Time diff between the past flow and the past-past flow.
                    T1 = last_ts - last_last_ts
                    # Time diff between the current flow and the past flow.
                    # We already computed this before, but we can do it here again just in case
                    T2 = now_ts - last_ts

                    # We have a time out of 1hs. After that, put 1 number 0 for each hs
                    # It should not happen that we also check T1... right?
                    if T2 >= tto.total_seconds():
                        t2_in_hours = T2 / tto.total_seconds()
                        # Shoud round it. Because we need the time to pass to really count it
                        # For example:
                        # 7100 / 3600 =~ 1.972  ->  int(1.972) = 1
                        for i in range(int(t2_in_hours)):
                            # Add the zeros to the symbol object
                            zeros += '0'

                    # Compute TD
                    try:
                        if T2 >= T1:
                            TD = T2 / T1
                        else:
                            TD = T1 / T2
                    except ZeroDivisionError:
                        TD = 1

                    # Decide the periodic based on TD and the thresholds
                    if TD <= tt1:
                        # Strongly periodicity
                        TD = 1
                    elif TD <= tt2:
                        # Weakly periodicity
                        TD = 2
                    elif TD <= tt3:
                        # Weakly not periodicity
                        TD = 3
                    elif TD > tt3:
                        # Strongly not periodicity
                        TD = 4
                self.print("Compute Periodicity: Profileid: {}, Tuple: {}, T1={}, T2={}, TD={}".format(profileid, tupleid, T1, T2, TD), 0, 5)
                return TD, zeros

            def compute_duration():
                """ Function to compute letter of the duration """
                if current_duration <= td1:
                    return 1
                elif current_duration > td1 and current_duration <= td2:
                    return 2
                elif current_duration > td2:
                    return 3

            def compute_size():
                """ Function to compute letter of the size """
                if current_size <= ts1:
                    return 1
                elif current_size > ts1 and current_size <= ts2:
                    return 2
                elif current_size > ts2:
                    return 3

            def compute_letter():
                """ Function to compute letter """
                if periodicity == -1:
                    if size == 1:
                        if duration == 1:
                            return '1'
                        elif duration == 2:
                            return '2'
                        elif duration == 3:
                            return '3'
                    elif size == 2:
                        if duration == 1:
                            return '4'
                        elif duration == 2:
                            return '5'
                        elif duration == 3:
                            return '6'
                    elif size == 3:
                        if duration == 1:
                            return '7'
                        elif duration == 2:
                            return '8'
                        elif duration == 3:
                            return '9'
                elif periodicity == 1:
                    if size == 1:
                        if duration == 1:
                            return 'a'
                        elif duration == 2:
                            return 'b'
                        elif duration == 3:
                            return 'c'
                    elif size == 2:
                        if duration == 1:
                            return 'd'
                        elif duration == 2:
                            return 'e'
                        elif duration == 3:
                            return 'f'
                    elif size == 3:
                        if duration == 1:
                            return 'g'
                        elif duration == 2:
                            return 'h'
                        elif duration == 3:
                            return 'i'
                elif periodicity == 2:
                    if size == 1:
                        if duration == 1:
                            return 'A'
                        elif duration == 2:
                            return 'B'
                        elif duration == 3:
                            return 'C'
                    elif size == 2:
                        if duration == 1:
                            return 'D'
                        elif duration == 2:
                            return 'E'
                        elif duration == 3:
                            return 'F'
                    elif size == 3:
                        if duration == 1:
                            return 'G'
                        elif duration == 2:
                            return 'H'
                        elif duration == 3:
                            return 'I'
                elif periodicity == 3:
                    if size == 1:
                        if duration == 1:
                            return 'r'
                        elif duration == 2:
                            return 's'
                        elif duration == 3:
                            return 't'
                    elif size == 2:
                        if duration == 1:
                            return 'u'
                        elif duration == 2:
                            return 'v'
                        elif duration == 3:
                            return 'w'
                    elif size == 3:
                        if duration == 1:
                            return 'x'
                        elif duration == 2:
                            return 'y'
                        elif duration == 3:
                            return 'z'
                elif periodicity == 4:
                    if size == 1:
                        if duration == 1:
                            return 'R'
                        elif duration == 2:
                            return 'S'
                        elif duration == 3:
                            return 'T'
                    elif size == 2:
                        if duration == 1:
                            return 'U'
                        elif duration == 2:
                            return 'V'
                        elif duration == 3:
                            return 'W'
                    elif size == 3:
                        if duration == 1:
                            return 'X'
                        elif duration == 2:
                            return 'Y'
                        elif duration == 3:
                            return 'Z'

            def compute_timechar():
                """ Function to compute the timechar """
                # self.print(f'Compute timechar. Profileid: {profileid} T2: {T2}', 0, 5)
                if not isinstance(T2, bool):
                    if T2 <= timedelta(seconds=5).total_seconds():
                        return '.'
                    elif T2 <= timedelta(seconds=60).total_seconds():
                        return ','
                    elif T2 <= timedelta(seconds=300).total_seconds():
                        return '+'
                    elif T2 <= timedelta(seconds=3600).total_seconds():
                        return '*'
                    else:
                        # Changed from 0 to ''
                        return ''
                else:
                    return ''

            # Here begins the function's code
            try:
                # Update value of T2
                if now_ts and last_ts:
                    T2 = now_ts - last_ts
                else:
                    T2 = False
                # Are flows sorted?
                if T2 < 0:
                    # Flows are not sorted!
                    # What is going on here when the flows are not ordered?? Are we losing flows?
                    # Put a warning
                    self.print("Warning: Coming flows are not sorted -> Some time diff are less than zero.", 0, 2)
                    pass
            except TypeError:
                T2 = False
            # self.print("T2:{}".format(T2), 0, 1)

            # Compute the rest
            periodicity, zeros = compute_periodicity(now_ts, last_ts, last_last_ts)
            duration = compute_duration()
            # self.print("Duration: {}".format(duration), 0, 1)
            size = compute_size()
            # self.print("Size: {}".format(size), 0, 1)
            letter = compute_letter()
            # self.print("Letter: {}".format(letter), 0, 1)
            timechar = compute_timechar()
            # self.print("TimeChar: {}".format(timechar), 0, 1)
            self.print("Profileid: {}, Tuple: {}, Periodicity: {}, Duration: {}, Size: {}, Letter: {}. TimeChar: {}".format(profileid, tupleid, periodicity, duration, size, letter, timechar), 0, 5)

            symbol = zeros + letter + timechar
            # Return the symbol, the current time of the flow and the T1 value
            return symbol, (last_ts, now_ts)
        except Exception as inst:
            # For some reason we can not use the output queue here.. check
            self.print("Error in compute_symbol in profilerProcess.", 0, 1)
            self.print("{}".format(type(inst)), 0, 1)
            self.print("{}".format(inst), 0, 1)
            self.print("{}".format(traceback.format_exc()), 0, 1)

    def get_timewindow(self, flowtime, profileid):
        """"
        This function should get the id of the TW in the database where the flow belong.
        If the TW is not there, we create as many tw as necessary in the future or past until we get the correct TW for this flow.
        - We use this function to avoid retrieving all the data from the DB for the complete profile. We use a separate table for the TW per profile.
        -- Returns the time window id
        THIS IS NOT WORKING:
        - The empty profiles in the middle are not being created!!!
        - The Dtp ips are stored in the first time win
        """
        try:
            # First check if we are not in the last TW. Since this will be the majority of cases
            try:
                [(lasttwid, lasttw_start_time)] = __database__.getLastTWforProfile(profileid)
                lasttw_start_time = float(lasttw_start_time)
                lasttw_end_time = lasttw_start_time + self.width
                flowtime = float(flowtime)
                self.print('The last TW id for profile {} was {}. Start:{}. End: {}'.format(profileid, lasttwid, lasttw_start_time, lasttw_end_time), 0, 4)
                # There was a last TW, so check if the current flow belongs here.
                if lasttw_end_time > flowtime and lasttw_start_time <= flowtime:
                    self.print("The flow ({}) is on the last time window ({})".format(flowtime, lasttw_end_time), 0, 4)
                    twid = lasttwid
                elif lasttw_end_time <= flowtime:
                    # The flow was not in the last TW, its NEWER than it
                    self.print("The flow ({}) is NOT on the last time window ({}). Its newer".format(flowtime, lasttw_end_time), 0, 4)
                    amount_of_new_tw = int((flowtime - lasttw_end_time) / self.width)
                    self.print("We have to create {} empty TWs in the midle.".format(amount_of_new_tw), 0, 4)
                    temp_end = lasttw_end_time
                    for id in range(0, amount_of_new_tw + 1):
                        new_start = temp_end
                        twid = __database__.addNewTW(profileid, new_start)
                        self.print("Creating the TW id {}. Start: {}.".format(twid, new_start), 0, 4)
                        temp_end = new_start + self.width
                    # Now get the id of the last TW so we can return it
                elif lasttw_start_time > flowtime:
                    # The flow was not in the last TW, its OLDER that it
                    self.print("The flow ({}) is NOT on the last time window ({}). Its older".format(flowtime, lasttw_end_time), 0, 4)
                    # Find out if we already have this TW in the past
                    data = __database__.getTWforScore(profileid, flowtime)
                    if data:
                        # We found a TW where this flow belongs to
                        (twid, tw_start_time) = data
                        return twid
                    else:
                        # There was no TW that included the time of this flow, so create them in the past
                        # How many new TW we need in the past?
                        # amount_of_new_tw is the total amount of tw we should have under the new situation
                        amount_of_new_tw = int((lasttw_end_time - flowtime) / self.width)
                        # amount_of_current_tw is the real amount of tw we have now
                        amount_of_current_tw = __database__.getamountTWsfromProfile(profileid)
                        # diff is the new ones we should add in the past. (Yes, we could have computed this differently)
                        diff = amount_of_new_tw - amount_of_current_tw
                        self.print("We need to create {} TW before the first".format(diff + 1), 0, 5)
                        # Get the first TW
                        [(firsttwid, firsttw_start_time)] = __database__.getFirstTWforProfile(profileid)
                        firsttw_start_time = float(firsttw_start_time)
                        # The start of the new older TW should be the first - the width
                        temp_start = firsttw_start_time - self.width
                        for id in range(0, diff + 1):
                            new_start = temp_start
                            # The method to add an older TW is the same as to add a new one, just the starttime changes
                            twid = __database__.addNewOlderTW(profileid, new_start)
                            self.print("Creating the new older TW id {}. Start: {}.".format(twid, new_start), 0, 2)
                            temp_start = new_start - self.width
            except ValueError:
                # There is no last tw. So create the first TW
                # If the option for only-one-tw was selected, we should create the TW at least 100 years before the flowtime, to cover for
                # 'flows in the past'. Which means we should cover for any flow that is coming later with time before the first flow
                if self.width == 9999999999:
                    # Seconds in 1 year = 31536000
                    startoftw = float(flowtime - (31536000 * 100))
                else:
                    startoftw = float(flowtime)
                # Add this TW, of this profile, to the DB
                twid = __database__.addNewTW(profileid, startoftw)
                #self.print("First TW ({}) created for profile {}.".format(twid, profileid), 0, 1)
            return twid
        except Exception as e:
            self.print("Error in get_timewindow().", 0, 1)
            self.print("{}".format(e), 0, 1)

    def run(self):
        # Main loop function
        try:
            rec_lines = 0
            while True:
                line = self.inputqueue.get()
                if 'stop' == line:
                    self.print("Stopping Profiler Process. Received {} lines ({})".format(rec_lines, datetime.now().strftime('%Y-%m-%d--%H:%M:%S')), 0, 2)
                    return True
                # if timewindows are not updated for a long time (see at logsProcess.py), we will stop slips automatically.The 'stop_process' line is sent from logsProcess.py.
                elif 'stop_process' in line:
                    self.print("Stopping Profiler Process. Received {} lines ({})", 0, 2)
                    return True
                else:
                    # Received new input data
                    # Extract the columns smartly
                    self.print("< Received Line: {}".format(line), 0, 3)
                    rec_lines += 1
                    if not self.input_type:
                        # Find the type of input received
                        # This line will be discarded because
                        self.define_type(line)
                        # We should do this before checking the type of input so we don't lose the first line of input

                    # What type of input do we have?
                    if self.input_type == 'zeek':
                        # self.print('Zeek line')
                        self.process_zeek_input(line)
                        # Add the flow to the profile
                        self.add_flow_to_profile()
                    elif self.input_type == 'argus':
                        # self.print('Argus line')
                        # Argus puts the definition of the columns on the first line only
                        # So read the first line and define the columns
                        try:
                            # Are the columns defined? Just try to access them
                            _ = self.column_idx['starttime']
                            # Yes
                            # Quickly process all lines
                            self.process_argus_input(line)
                            # Add the flow to the profile
                            self.add_flow_to_profile()
                        except AttributeError:
                            # No. Define columns. Do not add this line to profile, its only headers
                            self.define_columns(line)
                        except KeyError:
                            # When the columns are not there. Not sure if it works
                            self.define_columns(line)

                    elif self.input_type == 'suricata':
                        # self.print('Suricata line')
                        self.process_suricata_input(line)
                        # Add the flow to the profile
                        self.add_flow_to_profile()

                    elif self.input_type == 'zeek-tabs':
                        # self.print('Zeek-tabs line')
                        self.process_zeek_tabs_input(line)
                        # Add the flow to the profile
                        self.add_flow_to_profile()
                    elif self.input_type == 'nfdump':
                        self.process_nfdump_input(line)
                        self.add_flow_to_profile()
        except KeyboardInterrupt:
            self.print("Received {} lines.".format(rec_lines), 0, 1)
            return True
        except Exception as inst:
            self.print("Error. Stopped Profiler Process. Received {} lines".format(rec_lines), 0, 1)
            self.print("\tProblem with Profiler Process.", 0, 1)
            self.print(str(type(inst)), 0, 1)
            self.print(str(inst.args), 0, 1)
            self.print(str(inst), 0, 1)
            self.print(traceback.format_exc())
            return True
