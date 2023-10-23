# Must imports
import configparser

from slips.common.abstracts import Module
import multiprocessing
from slips.core.database import __database__
import platform

# Your imports
import json
import urllib3
import certifi
import time
import ipaddress


class VirusTotalModule(Module, multiprocessing.Process):
    # Name: short name of the module. Do not use spaces
    name = 'VirusTotal'
    description = 'IP address lookup on VirusTotal'
    authors = ['Dita Hollmannova']

    def __init__(self, outputqueue, config, testing=False):
        multiprocessing.Process.__init__(self)
        # All the printing output should be sent to the outputqueue, which is connected to OutputProcess
        self.outputqueue = outputqueue
        # In case you need to read the slips.conf configuration file for your own configurations
        self.config = config
        # Start the DB
        # This line might not be needed when running SLIPS, but when VT module is run standalone, it still uses the
        # database and this line is necessary. Do not delete it, instead move it to line 21.
        __database__.start(self.config)  # TODO: What does this line do? It changes nothing.

        # To which channels do you want to subscribe? When a message arrives on the channel the module will wake up
        # The options change, so the last list is on the slips/core/database.py file. However common options are:
        # - new_ip
        # - tw_modified
        # - evidence_added
        self.c1 = __database__.subscribe('new_flow')
        self.c2 = __database__.subscribe('new_dns_flow')
        # VT api URL for querying IPs
        self.url = 'https://www.virustotal.com/vtapi/v2/ip-address/report'
        # Read the conf file
        self.__read_configuration()
        self.key = None
        try:
            with open(self.key_file, "r") as f:
                self.key = f.read(64)
        except FileNotFoundError:
            self.print(
                f"The file with API key ({self.key_file}) could not be loaded. VT module is stopping."
            )

        # query counter for debugging purposes
        self.counter = 0

        # Pool manager to make HTTP requests with urllib3
        # The certificate provides a bundle of trusted CAs, the certificates are located in certifi.where()
        self.http = urllib3.PoolManager(cert_reqs="CERT_REQUIRED", ca_certs=certifi.where())
        # Set the timeout based on the platform. This is because the pyredis lib does not have officially recognized the timeout=None as it works in only macos and timeout=-1 as it only works in linux
        if platform.system() == 'Darwin':
            # macos
            self.timeout = None
        elif platform.system() == 'Linux':
            # linux
            self.timeout = None
        else:
            #??
            self.timeout = None

    def __read_configuration(self) -> str:
        """ Read the configuration file for what we need """
        # Get the time of log report
        try:
            self.key_file = self.config.get("virustotal", "api_key_file")
        except (configparser.NoOptionError, configparser.NoSectionError, NameError):
            # There is a conf, but there is no option, or no section or no configuration file specified
            self.key_file = None
        try:
            # update period
            self.update_period = self.config.get('virustotal', 'virustotal_update_period')
            self.update_period = float(self.update_period)
        except (configparser.NoOptionError, configparser.NoSectionError, NameError):
            # There is a conf, but there is no option, or no section or no configuration file specified
            self.update_period = 259200

    def print(self, text, verbose=1, debug=0):
        """
        Function to use to print text using the outputqueue of slips.
        Slips then decides how, when and where to print this text by taking all the processes into account

        Input
         verbose: is the minimum verbosity level required for this text to be printed
         debug: is the minimum debugging level required for this text to be printed
         text: text to print. Can include format like 'Test {}'.format('here')

        If not specified, the minimum verbosity level required is 1, and the minimum debugging level is 0
        """

        vd_text = str(int(verbose) * 10 + int(debug))
        self.outputqueue.put(f'{vd_text}|{self.name}|[{self.name}] {str(text)}')

    def run(self):
        if self.key is None:
            # We don't have a virustotal key
            return
        try:
            # Main loop function
            while True:
                message_c1 = self.c1.get_message(timeout=0.01)
                # if timewindows are not updated for a long time we will stop slips automatically.
                if message_c1 and message_c1['data'] == 'stop_process':
                    return True
                if message_c1 and message_c1['channel'] == 'new_flow' and message_c1["type"] == "message":
                    data = message_c1["data"]
                    if type(data) == str:
                        data = json.loads(data)
                        profileid = data['profileid']
                        twid = data['twid']
                        stime = data['stime']
                        flow = json.loads(data['flow']) # this is a dict {'uid':json flow data}
                        # there is only one pair key-value in the dictionary
                        for key, value in flow.items():
                            uid = key
                            flow_data = json.loads(value)
                        ip = flow_data['daddr']
                        #ip = data_flow_dict['saddr']
                        # The first message comes with data=1
                        data = __database__.getIPData(ip)
                        # return an IPv4Address or IPv6Address object depending on the IP address passed as argument.
                        ip_addr = ipaddress.ip_address(ip)
                        # If we already have the VT for this ip, do not ask VT
                        # Check that there is data in the DB, and that the data is not empty, and that our key is not there yet
                        # Check if the ip is multicast, if yes, then fdo not process
                        if (data or data == {}) and 'VirusTotal' not in data and not ip_addr.is_multicast:
                            vt_scores, passive_dns, as_owner = self.get_ip_vt_data(ip)
                            vtdata = {"URL": vt_scores[0],
                                      "down_file": vt_scores[1],
                                      "ref_file": vt_scores[2],
                                      "com_file": vt_scores[3],
                                      "timestamp": time.time()}
                            data = {}
                            data["VirusTotal"] = vtdata

                            # Add asn if it is unknown or not in the IP info
                            if 'asn' not in data or data['asn'] == 'Unknown':
                                data['asn'] = as_owner

                            __database__.setInfoForIPs(ip, data)
                            __database__.set_passive_dns(ip, passive_dns)

                        elif data and 'VirusTotal' in data:
                            # If VT is in data, check timestamp. Take time difference, if not valid, update vt scores.
                            if (time.time() - data["VirusTotal"]['timestamp']) > self.update_period:
                                vt_scores, passive_dns, _ = self.get_ip_vt_data(ip)
                                vtdata = {"URL": vt_scores[0],
                                          "down_file": vt_scores[1],
                                          "ref_file": vt_scores[2],
                                          "com_file": vt_scores[3],
                                          "timestamp": time.time()}
                                data = {}
                                data["VirusTotal"] = vtdata
                                __database__.setInfoForIPs(ip, data)
                                __database__.set_passive_dns(ip, passive_dns)

                message_c2 = self.c2.get_message(timeout=0.01)
                if message_c2 and message_c2['data'] == 'stop_process':
                    return True
                if message_c2 and message_c2['channel'] == 'new_dns_flow' and message_c2["type"] == "message":
                    data = message_c2["data"]
                    # The first message comes with data=1
                    if type(data) == str:
                        data = json.loads(data)
                        profileid = data['profileid']
                        twid = data['twid']
                        flow_data = json.loads(data['flow']) # this is a dict {'uid':json flow data}
                        domain = flow_data['query']
                        data = __database__.getDomainData(domain)
                        # If we already have the VT for this ip, do not ask VT
                        # Check that there is data in the DB, and that the data is not empty, and that our key is not there yet
                        if (data or data == {}) and 'VirusTotal' not in data:
                            vt_scores, as_owner = self.get_domain_vt_data(domain)
                            vtdata = {"URL": vt_scores[0],
                                      "down_file": vt_scores[1],
                                      "ref_file": vt_scores[2],
                                      "com_file": vt_scores[3],
                                      "timestamp": time.time()}
                            data = {}
                            data["VirusTotal"] = vtdata

                            # Add asn if it is unknown or not in the IP info
                            if 'asn' not in data or data['asn'] == 'Unknown':
                                data['asn'] = as_owner

                            __database__.setInfoForDomains(domain, data)

                        elif data:
                            # If VT is in data, check timestamp. Take time difference, if not valid, update vt scores.
                            if (time.time() - data["VirusTotal"]['timestamp']) > self.update_period:
                                vt_scores, _ = self.get_domain_vt_data(domain)
                                vtdata = {"URL": vt_scores[0],
                                          "down_file": vt_scores[1],
                                          "ref_file": vt_scores[2],
                                          "com_file": vt_scores[3],
                                          "timestamp": time.time()}
                                data = {}
                                data["VirusTotal"] = vtdata
                                __database__.setInfoForDomains(domain, data)

        except KeyboardInterrupt:
            return True
        except Exception as inst:
            self.print('Problem on the run()', 0, 1)
            self.print(str(type(inst)), 0, 1)
            self.print(str(inst.args), 0, 1)
            self.print(str(inst), 0, 1)
            return True

    def get_as_owner(self, response):
        """
        Get as owner of the IP
        :param response: json dictionary with response data
        """
        response_key = 'as_owner'
        return response[response_key] if response_key in response else ''

    def get_passive_dns(self,response):
        """
        Get passive dns from virustotal response
        :param response: json dictionary with response data
        """
        response_key = 'resolutions'
        return response[response_key] if response_key in response else ''

    def get_ip_vt_data(self, ip: str):
        """
        Look if this IP was already processed. If not, perform API call to VirusTotal and return scores for each of
        the four processed categories. Response is cached in a dictionary. Private IPs always return (0, 0, 0, 0).
        :param ip: IP address to check
        :return: 4-tuple of floats: URL ratio, downloaded file ratio, referrer file ratio, communicating file ratio 
        """

        try:
            addr = ipaddress.ip_address(ip)
            if addr.is_private:
                self.print(f"[{ip}] is private, skipping", 5, 3)
                scores = 0,0,0,0
                return scores, '', ''

            # for unknown address, do the query
            response = self.api_query_(ip)
            as_owner = self.get_as_owner(response)
            passive_dns = self.get_passive_dns(response)
            scores = interpret_response(response)
            self.counter += 1
            return scores, passive_dns, as_owner
        except Exception as inst:
            self.print('Problem in the get_ip_vt_data()', 0, 1)
            self.print(str(type(inst)), 0, 1)
            self.print(str(inst.args), 0, 1)
            self.print(str(inst), 0, 1)

    def get_domain_vt_data(self, domain: str):
        """
        Look if this domain was already processed. If not, perform API call to VirusTotal and return scores for each of
        the four processed categories. Response is cached in a dictionary.
        :param domain: Domain address to check
        :return: 4-tuple of floats: URL ratio, downloaded file ratio, referrer file ratio, communicating file ratio
        """

        try:
            # for unknown address, do the query
            response = self.api_query_(domain)
            as_owner = self.get_as_owner(response)
            scores = interpret_response(response)
            self.counter += 1
            return scores, as_owner
        except Exception as inst:
            self.print('Problem in the get_domain_vt_data()', 0, 1)
            self.print(str(type(inst)), 0, 1)
            self.print(str(inst.args), 0, 1)
            self.print(str(inst), 0, 1)

    def api_query_(self, ip, save_data=False):
        """
        Create request and perform API call
        :param ip: IP address to check
        :param save_data: False by default. Set to True to save each request json in a file named ip.txt
        :return: Response object
        """

        params = {'apikey': self.key, 'ip': ip}

        # wait for network
        while True:
            try:
                response = self.http.request("GET", self.url, fields=params)
                break
            except urllib3.exceptions.MaxRetryError:
                self.print("Network is not available, waiting 10s")
                time.sleep(10)

        sleep_attempts = 0

        # repeat query if API limit was reached (code 204)
        while response.status != 200:

            # requests per minute limit reached
            if response.status == 204:
                # usually sleeping for 40 seconds is enough, if not, try adding 20 more
                sleep_time = 40 if sleep_attempts == 0 else 20
                sleep_attempts += 1

            elif response.status == 403:
                # 10 minutes
                sleep_time = 600
                self.print("Please check that your API key is correct. Code 403 means timeout but also wrong API key.")

            else:
                # if the query was unsuccessful but it is not caused by API limit, abort (this is some unknown error)
                # X-Api-Message is a comprehensive error description, but it is not always present
                if "X-Api-Message" in response.headers:
                    message = response.headers["X-Api-Message"]
                # Reason is a much shorter description ("Forbidden"), but it is always there
                else:
                    message = response.reason
                raise Exception(
                    f"VT API returned unexpected code: {str(response.status)} - {message}"
                )

            # report that API limit is reached, wait one minute and try again
            self.print(
                f"Status code is {str(response.status)} at {str(time.asctime())}, query id: {str(self.counter)}",
                verbose=5,
            )

            self.print(
                f"API limit reached, going to sleep for {str(sleep_time)} seconds",
                verbose=1,
                debug=1,
            )
            time.sleep(sleep_time)
            response = self.http.request("GET", self.url, fields=params)

        data = json.loads(response.data)

        # optionally, save data to file
        if save_data:
            if filename := f"{ip}.txt":
                with open(filename, 'w') as f:
                    json.dump(data, f)

        return data


def interpret_response(response: dict):
    """
    Read the dictionary and compute ratio for each category.
    
    The ratio is computed as follows:
    For each IP, VT API returns data about four categories: URLs that resolved to the IP, samples (files) downloaded
    from the IP, samples (files) that contain the given IP, and samples (programs) that contact the IP. The structure of
    the data is same for all four categories.
    
    For each sample in a category, VT asks the antivirus engines and counts how many of them find the sample malicious.
    For example, if VT asked 27 engines and four of them found the sample malicious, the sample would be given score
    4/27, where 4 is the number of successful detections, and 27 is the total number of engines used.
    
    The response has two fields for each category. These are the "detected_<category>" field, which contains list of
    samples that were found malicious by at least one engine, and the "undetected_<category>" field, which contains all 
    the samples that none of the engines found malicious (all samples in undetected have score 0/x). This means that the
    response has 8 fields with scores - two (detected and undetected) for each of the four categories. Some fields may
    be missing if data for the category is not present.
    
    To compute the ratio for a category, scores across the two fields are summed together. A global number of detections
    is computed (sum of all positive detections across all samples in the detected field) and the global number of tests
    is computed (sum of all "total" values in both detected and undetected sample lists). Now we have detections for a 
    category and total for a category. The ratio for a category is detections/total. If no tests were run (the field is
    empty and total=0), this would be undefined, so the ratio is set to 0.
    
    Ratio is computed separately for each category.
    
    :param response: dictionary (json data from the response)
    :return: four floats: url_ratio, down_file_ratio, ref_file_ratio, com_file_ratio
    """

    # compute how many tests were run on the undetected samples. This will return tuple (0, total)
    # the numbers 2 and 3 are keys to the dictionary, which is in this only case (probably by mistake) a list
    undetected_url_score = count_positives(response, "undetected_urls", 2, 3)

    # compute how many tests were run on the detected samples. This will return tuple (detections, total)
    detected_url_score = count_positives(response, "detected_urls", "positives", "total")

    # sum the previous results, to get the sum of detections and sum of total tests
    url_detections = undetected_url_score[0] + detected_url_score[0]
    if url_total := undetected_url_score[1] + detected_url_score[1]:
        url_ratio = url_detections/url_total
    else:
        url_ratio = 0

    # following categories are computed in the same way
    undetected_download_score = count_positives(response, "undetected_downloaded_samples", "positives", "total")
    detected_download_score = count_positives(response, "detected_downloaded_samples", "positives", "total")
    down_file_detections = undetected_download_score[0] + detected_download_score[0]
    if (
        down_file_total := undetected_download_score[1]
        + detected_download_score[1]
    ):
        down_file_ratio = down_file_detections/down_file_total
    else:
        down_file_ratio = 0

    undetected_ref_score = count_positives(response, "undetected_referrer_samples", "positives", "total")
    detected_ref_score = count_positives(response, "detected_referrer_samples", "positives", "total")
    ref_file_detections = undetected_ref_score[0] + detected_ref_score[0]
    if ref_file_total := undetected_ref_score[1] + detected_ref_score[1]:
        ref_file_ratio = ref_file_detections/ref_file_total
    else:
        ref_file_ratio = 0

    undetected_com_score = count_positives(response, "undetected_communicating_samples", "positives", "total")
    detected_com_score = count_positives(response, "detected_communicating_samples", "positives", "total")
    com_file_detections = undetected_com_score[0] + detected_com_score[0]
    if com_file_total := undetected_com_score[1] + detected_com_score[1]:
        com_file_ratio = com_file_detections/com_file_total
    else:
        com_file_ratio = 0

    # Convert the values into percentages before returning
    url_ratio = url_ratio * 100
    down_file_ratio = down_file_ratio * 100
    ref_file_ratio = ref_file_ratio * 100
    com_file_ratio = com_file_ratio * 100

    return url_ratio, down_file_ratio, ref_file_ratio, com_file_ratio


def count_positives(response: dict, response_key: str, positive_key, total_key):
    """
    Count positive checks and total checks in the response, for the given category. To compute ratio of downloaded
    samples, sum results for both detected and undetected dicts: "undetected_downloaded_samples" and
    "detected_downloaded_samples".
    :param response: json dictionary with response data
    :param response_key: category to count, eg "undetected_downloaded_samples"
    :param positive_key: key to use inside of the category for successful detections (usually its "positives")
    :param total_key: key to use inside of the category to sum all checks (usually its "total")
    :return: number of positive tests, number of total tests run
    """
    detections = 0
    total = 0
    if response_key in response:
        for item in response[response_key]:
            detections += item[positive_key]
            total += item[total_key]
    return detections, total
