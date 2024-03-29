from .models import *
import logging, pathlib, os, subprocess, sys
from termcolor import colored
import tempfile
from datetime import datetime
import string
import random
from typing import List, Callable, Tuple
from .testcases import *
import json
import time
import statistics
import shutil
from collections import defaultdict
import glob
import os
import prettytable
from pprint import pprint

current_script_directory = os.path.dirname(os.path.abspath(__file__))
project_root = os.path.normpath(os.path.join(os.path.join(current_script_directory, os.pardir), os.pardir))

def random_string(length: int):
    """ Generate a random string of fixed length """
    letters = string .ascii_lowercase
    return "".join(random.choice(letters) for i in range(length))

def get_tests_and_measurements(config: YamlConfig) -> Tuple[List[TestCase], List[TestCase]]:
    tests = [] # Ignored for now as test would be to check if par example a handshake is successful etc. For now we are focused on the measurements
    measurements = []
    for measurement_metric in config.measurement_metrics:
        if measurement_metric not in MEASUREMENTS.keys():
            raise ValueError(f"Unknown measurement metric: {measurement_metric}")
        measurement = MEASUREMENTS[measurement_metric]
        measurement.FILESIZE = config.filesize
        measurement.REPETITIONS = config.repetitions
        measurements.append(measurement)
    return tests, measurements

class PerfomanceRunner:

    def __init__(
        self, 
        testbed: TestbedConfig, 
        config: YamlConfig, 
        debug=True
    ) -> None:
        self._testbed = testbed
        self._config = config
        self.logger = logging.getLogger()
        self._init_logger(debug)
        self._implementations_directory = os.path.join(
            project_root, 'implementations'
        )
        self.compliant_checks = dict()
        self._venv_dir = "/tmp"
        self._disable_client_aes_offload = False
        self._disable_server_aes_offload = False
        self._save_files = False
        self._start_time = datetime.now()
        self._tests, self._measurements = get_tests_and_measurements(self._config) 
        self._prepared_envs = set()
        self.test_results = {}
        self.measurement_results = defaultdict(dict)
        self._continue_on_error = False
        self._log_dir = os.path.join(project_root, "logs", "logs_{:%Y-%m-%dT%H:%M:%S}".format(self._start_time))
        self._built_executables = set()
        self._output = ""

    def _init_logger(self, debug) -> None:
        self.logger.setLevel(logging.DEBUG)
        console = logging.StreamHandler(stream=sys.stderr)
        formatter = logging.Formatter(
            '[%(asctime)s][%(levelname)s]: %(message)s'
        )
        console.setFormatter(formatter)
        if debug:
            console.setLevel(logging.DEBUG)
        else:
            console.setLevel(logging.INFO)
        self.logger.addHandler(console)

    def _push_directory_to_remote(self, host: str, src, dst=None, normalize=True):
        """Copies a directory <src> from the machine it is executed on
        (management host) to a given host <host> to path <dst> using rsync.
        """
        if normalize:
            src = os.path.normpath(src)

        if not dst:
            dst = str(pathlib.Path(src).parent)
        self.logger.debug(f"Copy {src} to {host}:{dst}")

        cmd = f'rsync -r {src} {host}:{dst}'
        p = subprocess.Popen(
            cmd,
            shell=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE
        )
        try:
            returned_code = p.wait()
            self.logger.debug(f"The transfer return code : {returned_code}")
        except subprocess.TimeoutExpired:
            self.logger.debug(
                f'Timeout when moving files {src} to host {host}')
        return

    def _generate_cert_chain(self, directory: str, length: int = 1):
        cmd = f"{current_script_directory}/certs.sh " + directory + " " + str(length)
        r = subprocess.run(
            cmd, shell=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT
        )
        if r.returncode != 0:
            self.logger.error(colored("Error generating certificates", "red", attrs=["bold"]))
            self.logger.error("%s", r.stdout.decode("utf-8"))
            sys.exit(r.returncode)
        else:
            self.logger.debug("%s", r.stdout.decode("utf-8"))

    def _give_execute_permission(self, host: Host, script: str) -> None:
        self.logger.debug(f"Give execute permission to {script} on {host.hostname}")
        cmd = f'ssh {host.hostname} "chmod +x {script}"'
        p = subprocess.Popen(
            cmd,
            shell=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE
        )
        try:
            returned_code = p.wait()
            self.logger.debug(f"The command return code : {returned_code}")
        except subprocess.TimeoutExpired:
            self.logger.error(
                colored(f'Timeout when moving files {script} to host {host.hostname}', 'red')
            )
        return
    
    def _copy_implementations(self):
        for implementation in self._config.implementations:
            self._push_directory_to_remote(
                self._testbed.client.hostname,
                os.path.join(
                    self._implementations_directory,
                    implementation,
                    ''  # This prevents that rsync copies the directory into itself, adds trailing slash
                ),
                f"~",
                normalize=True
            )
            self._push_directory_to_remote(
                self._testbed.server.hostname,
                os.path.join(
                    self._implementations_directory,
                    implementation,
                    ''  # This prevents that rsync copies the directory into itself, adds trailing slash
                ),
                f"~",
                normalize=True
            )
            for host in [self._testbed.client, self._testbed.server]:
                shell_scripts = glob.glob(os.path.join(self._implementations_directory, implementation, "*.sh"))
                shell_scripts = [f"~/{implementation}/{os.path.basename(script)}" for script in shell_scripts]
                for script in shell_scripts:
                    self._give_execute_permission(host, script)

    def _does_remote_file_exist(self, host: Host, file: str) -> bool:
        self.logger.debug(f"Checking if {file} exists on {host.hostname}")

        check = subprocess.Popen(
            f'ssh {host.hostname} "test -f {file}"',
            shell=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE
        )

        try:
            exit_code = check.wait(timeout=10)
            return exit_code == 0
        except subprocess.TimeoutExpired:
            return False
        
    def _delete_remote_directory(self, host: Host, directory: str) -> None:
        cmd = f'ssh {host.hostname} "rm -rf {directory}"'
        self.logger.debug(f"Deleting {host.hostname}:{directory}")

        subprocess.Popen(
            cmd,
            shell=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE
        )

    def _run_command_on_remote_host(self, host: Host, command: list) -> None:
        command_string = " ".join(command)
        self.logger.debug(f"Running command \"{command_string}\" on {host.hostname}")
        proc = subprocess.Popen(
            f'ssh {host.hostname} "{command_string}"',
            shell=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE
        )
        stdout, stderr = proc.communicate()
        self.logger.debug(host.hostname)
        self.logger.debug(colored(f"stdout: {stdout.decode('utf-8')}", "yellow"))
        self.logger.debug(colored(f"stderr: {stderr.decode('utf-8')}", "yellow"))

    def _create_venv_on_remote_host(self, host: Host, venv_dir_path: str) -> None:
        self.logger.debug(f"Venv Setup: Creating venv on host {host.hostname} at {venv_dir_path}")
        self._run_command_on_remote_host(
            host,
            ["python3", "-m", "venv", venv_dir_path]
        )
        self._give_execute_permission(host, f"{venv_dir_path}/bin/activate")
    
    def _get_venv(self, host: Host) -> str:
            """Creates the venv directory for the specified role either locally or
            copied to the host in testbed mode should it not exist.

            Return: the path of the
            with a prepended bash 'source' command.
            """
            venv_dir = os.path.join(self._venv_dir, host.role)
            venv_activate = os.path.join(venv_dir, "bin/activate")

            if not self._does_remote_file_exist(host, venv_activate):
                self._create_venv_on_remote_host(host, venv_dir)
            return venv_activate
    
    def _is_unsupported(self, lines: List[str]) -> bool:
        return any("exited with code 127" in str(line) for line in lines) or any(
            "exit status 127" in str(line) for line in lines
        )
    
    def _check_compliance(self, implementation: str, host: Host) -> bool:
        assert host.role in ['client', 'server'], "Host role must be either 'client' or 'server'"
        if host.role in self.compliant_checks:
            self.logger.debug(
                f"Already checked compliance for {host.role}"
            )
            return self.compliant_checks[host.role]
        
        log_dir = tempfile.TemporaryDirectory(dir="/tmp", prefix="logs_")
        www_dir = tempfile.TemporaryDirectory(dir="/tmp", prefix="compliance_www_")
        certs_dir = tempfile.TemporaryDirectory(dir="/tmp", prefix="compliance_certs_")
        downloads_dir = tempfile.TemporaryDirectory(
            dir="/tmp", prefix="compliance_downloads_"
        )

        script_to_run = f"~/{implementation}/run-{host.role}.sh"
        
        if not self._does_remote_file_exist(host, script_to_run):
            self.logger.error(
                colored(f"{script_to_run} does not exist on {host.hostname}", "red")
            )
            return False
        
        self._generate_cert_chain(certs_dir.name)

        for dir in [log_dir.name, www_dir.name, certs_dir.name, downloads_dir.name]:
            self._push_directory_to_remote(
                host.hostname,
                dir
            )

        venv_script = self._get_venv(host)

        params = " ".join([
            f"TESTCASE={random_string(8)}",
            f"DOWNLOADS={downloads_dir.name}",
            f"LOGS={log_dir.name}",
            f"QLOGDIR={log_dir.name}",
            f"SSLKEYLOGFILE={log_dir.name}/keys.log",
            f"IP=localhost",
            f"PORT=4433",
            f"CERTS={certs_dir.name}",
            f"WWW={www_dir.name}",
        ])

        cmd = f"{venv_script}; {params} ./run-{host.role}.sh"
        cmd = f'ssh {host.hostname} \'cd {implementation}; {cmd}\''
        
        self.logger.debug(cmd)

        proc = subprocess.Popen(
            cmd,
            shell=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT
        )

        try:
            stdout, stderr = proc.communicate(timeout=10)
        except subprocess.TimeoutExpired:
            self.logger.error(colored(f"{host.hostname} {host.role} compliance check timeout", 'red'))
            self.logger.error(colored(f"{host.hostname} {host.role} not compliant", 'red'))
            self.compliant_checks[host.role] = False
            return False
        finally:
            self._delete_remote_directory(host, log_dir.name)
            self._delete_remote_directory(host, www_dir.name)
            self._delete_remote_directory(host, certs_dir.name)
            self._delete_remote_directory(host, downloads_dir.name)

        if not proc.returncode == 127 and not self._is_unsupported(stdout.decode("utf-8").splitlines()):
            self.logger.error(colored(f"{host.hostname} {host.role} not compliant", 'red'))
            self.logger.debug("%s", stdout.decode("utf-8"))
            self.compliant_checks[host.role] = False
            return False
        
        self.logger.debug(f"{host.hostname} {host.role} compliant.")
        self.compliant_checks[host.role] = True
        return True
    
    def _log_process(self, stdout: bytes, stderr: bytes, context: str) -> None:
        self.logger.debug(context)
        self.logger.debug(colored(f"\nstdout: {stdout.decode('utf-8')}", "yellow"))
        self.logger.debug(colored(f"\nstderr: {stderr.decode('utf-8')}", "yellow"))
    
    def _run_script_on_machine(self, host: Host, script_path: str):
        self.logger.debug(f'Running {script_path} on {host.hostname}')
        proc = subprocess.Popen(
            f'cat {script_path} | ssh {host.hostname} "sudo bash -s"',
            shell=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE
        )
        try: 
            stdout, stderr = proc.communicate()
            self._log_process(stdout, stderr, f'host: {host.hostname}')
        except subprocess.TimeoutExpired:
            self.logger.debug(
                colored(f'Timeout when running {script_path} on host {host.hostname}', 'yellow')
            )
            return
        
    def _run_prepost_runscript(self, host: Host, script: PrePostRunScript):
        self.logger.debug(f'Running {script.script} on {host.hostname}')
        proc = subprocess.Popen(
            f'cat {script.script} | ssh {host.hostname} "sudo bash -s"',
            shell=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE
        )
        try:
            if not script.blocking:
                stdout, stderr = proc.communicate()
                self._log_process(stdout, stderr, f'host: {host.hostname}')
        except Exception as e:
            self.logger.error(
                colored(f'Error{e} when running {script.script} on host {host.hostname}', 'red')
            )
        finally: 
            return proc
        
    def _setup_hosts(self):
        self._run_script_on_machine(self._testbed.server, os.path.join(current_script_directory, "setup.sh"))
        self._run_script_on_machine(self._testbed.client, os.path.join(current_script_directory, "setup.sh"))

        for implementation in self._config.implementations:
            self._setup_env(self._testbed.client, "~/" + implementation)
            self._setup_env(self._testbed.server, "~/" + implementation)

    def _check_impl_compliance(self, implementation: str) -> bool:
        return self._check_compliance(implementation, self._testbed.server) and self._check_compliance(implementation, self._testbed.client)
    
    def _set_variables_on_machine(self, host: Host, dictionary: dict):
        self.logger.debug(f"Setting the variables:\n{dictionary}\non the host {host.hostname}")

        tmp_file = tempfile.NamedTemporaryFile(dir="/tmp", prefix="interop-temp-data-", mode="w+")
        json.dump(dictionary, tmp_file, ensure_ascii=False, indent=4)
        tmp_file.flush()
        tmp_file.seek(0)

        src = tmp_file.name
        dst = "/tmp/interop-variables.json"
        self.logger.debug(f"Copy {src} to {host.hostname}:{dst}")
        cmd = f'rsync -r {src} {host.hostname}:{dst}'

        p = subprocess.Popen(
            cmd,
            shell=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE
        )
        try:
            if p.wait(10) != 0:
                self.logger.error(colored(f"Failed to copy {src} to {host.hostname}:{dst}", "red"))
                self.logger.error(colored(f"{p.stdout.read().decode('utf-8')}", "red"))
                self.logger.error(colored(f"{p.stderr.read().decode('utf-8')}", "red")) 
        except subprocess.TimeoutExpired:
            self.logger.debug(
                f'Timeout when moving variable file to host {host.hostname}')
        finally:
            tmp_file.close()
            return

    def _remove_all_variables_from_machine(self, host: Host):
        self.logger.debug(f"Removing all variables from {host.hostname}")
        prog = subprocess.Popen(
            f'ssh {host.hostname} "rm -rf /tmp/interop-variables.json"',
            shell=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE
        )
        return prog.wait(10)
    
    def _pull_directory_from_remote(self, host: Host, src: str, dst=None):
        src = os.path.normpath(src)
        if not dst:
            dst = str(pathlib.Path(src).parent)
        self.logger.debug(f"Copy {host.hostname}:{src} to {dst}")

        cmd = f'rsync -r {host.hostname}:{src} {dst}'
        p = subprocess.Popen(
            cmd,
            shell=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE
        )
        try:
            # large timeout as copied file could be very large
            p.wait(2000)
        except subprocess.TimeoutExpired:
            self.logger.debug(
                f'Timeout when copying files {src} from host {host.hostname}')
        return
    
    def _build_impl_executable(self, host: Host, implementation: str) -> None:
        self.logger.debug(f"Building {implementation} on {host.hostname}")
        self._run_command_on_remote_host(host, [f"cd ~/{implementation}; ./{self._config.build_script}"])
        self._give_execute_permission(host, f"~/{implementation}/{implementation}-{host.role}")

    def _run_testcase(self, implementation_name: str, server: Host, client: Host, test: Callable[[], TestCase], log_dir_prefix=None) -> None:
        start_time = datetime.now()
        sim_log_dir = tempfile.TemporaryDirectory(dir="/tmp", prefix="logs_sim_")
        server_log_dir = tempfile.TemporaryDirectory(dir="/tmp", prefix="logs_server_")
        client_log_dir = tempfile.TemporaryDirectory(dir="/tmp", prefix="logs_client_")
        log_file = tempfile.NamedTemporaryFile(dir="/tmp", prefix="output_log_")
        log_handler = logging.FileHandler(log_file.name)
        log_handler.setLevel(logging.DEBUG)

        formatter = LogFileFormatter("%(asctime)s %(message)s")
        log_handler.setFormatter(formatter)
        self.logger.addHandler(log_handler)

        client_keylog = os.path.join(client_log_dir.name, 'keys.log')
        server_keylog = os.path.join(server_log_dir.name, 'keys.log')

        client_qlog_dir = os.path.join(client_log_dir.name, 'client_qlog/')
        server_qlog_dir = os.path.join(server_log_dir.name, 'server_qlog/')
        
        testcase: TestCase = test(
            link_bandwidth=None,
            client_delay=None,
            server_delay=None,
            sim_log_dir=sim_log_dir,
            client_keylog_file=client_keylog,
            server_keylog_file=server_keylog,
            client_log_dir=client_log_dir.name,
            server_log_dir=server_log_dir.name,
            client_qlog_dir=client_qlog_dir,
            server_qlog_dir=server_qlog_dir,
            server_ip=server.ip,
            server_name=server.role
        )

        for dir in [server_log_dir.name, testcase.www_dir(), testcase.certs_dir()]:
            self._push_directory_to_remote(server.hostname, dir)
        for dir in [sim_log_dir.name, client_log_dir.name, testcase.download_dir()]:
            self._push_directory_to_remote(client.hostname, dir)

        paths = testcase.get_paths(
            host=server.hostname
        )

        reqs = " ".join([testcase.urlprefix() + p for p in paths])
        self.logger.debug("Requests: %s", reqs)

        server_params = " ".join([
            f"SSLKEYLOGFILE={server_keylog}",
            f"QLOGDIR={server_qlog_dir}" if testcase.use_qlog() else "",
            f"LOGS={server_log_dir.name}",
            f"TESTCASE={testcase.testname(Perspective.SERVER)}",
            f"WWW={testcase.www_dir()}",
            f"CERTS={testcase.certs_dir()}",
            f"IP={testcase.ip()}",
            f"PORT={testcase.port()}",
            f"SERVERNAME={testcase.servername()}",
        ])
        if self._disable_server_aes_offload:
            server_params = " ".join([
                'OPENSSL_ia32cap="~0x200000200000000"',
                server_params
            ])

        client_params = " ".join([
            f"SSLKEYLOGFILE={client_keylog}",
            f"QLOGDIR={client_qlog_dir}" if testcase.use_qlog() else "",
            f"LOGS={client_log_dir.name}",
            f"TESTCASE={testcase.testname(Perspective.CLIENT)}",
            f"DOWNLOADS={testcase.download_dir()}",
            f"REQUESTS=\"{reqs}\"",
        ])
        if self._disable_client_aes_offload:
            client_params = " ".join([
                'OPENSSL_ia32cap="~0x200000200000000"',
                client_params
            ])



        if implementation_name not in self._built_executables:
            self._build_impl_executable(server, implementation_name)
            self._built_executables.add(implementation_name)
            self._build_impl_executable(client, implementation_name)
            self._built_executables.add(implementation_name)

        
        server_run_script = "./run-server.sh"
        server_venv_script = self._get_venv(server)
        client_run_script = "./run-client.sh"
        client_venv_script = self._get_venv(client)

        server_cmd = f"{server_venv_script}; {server_params} {server_run_script}"
        client_cmd = f"{client_venv_script}; {client_params} {client_run_script}"

        server_cmd = f'ssh {server.hostname} \'cd ~/{implementation_name}; {server_cmd}\''
        client_cmd = f'ssh {client.hostname} \'cd ~/{implementation_name}; {client_cmd}\''
        expired = False

        try:
            # TODO add ifstat 
            # TODO add adding delays with tc
            server_variables: dict = {
                "implementation": implementation_name,
                "interfaces": server.interfaces,
                "hostname": server.hostname,
                "log_dir": server_log_dir.name,
                "www_dir": testcase.www_dir(),
                "certs_dir": testcase.certs_dir(),
                "role": server.role,
            }
            server_variables = {**server_variables, **self._config.server_implementation_params}
            client_variables: dict = {
                "implementation": implementation_name,
                "interfaces": client.interfaces,
                "hostname": client.hostname,
                "log_dir": client_log_dir.name,
                "sim_log_dir": sim_log_dir.name,
                "download_dir": testcase.download_dir(),
                "role": client.role,
            }
            client_variables = {**client_variables, **self._config.client_implementation_params}

            # TODO add server_implementation_params and client_implementation_params
            self._set_variables_on_machine(
                server,
                server_variables
            )
            self._set_variables_on_machine(
                client,
                client_variables
            )

            server_scripts_run: List[Tuple[PrePostRunScript, subprocess.Popen]] = []
            client_scripts_run: List[Tuple[PrePostRunScript, subprocess.Popen]] = []

            # Execute List of Server Pre Run Scripts if given
            for server_script in self._config.server_prerunscript:
                if not server_script.blocking:
                    self._run_script_on_machine(server, server_script.script)
                else:
                    server_scripts_run.append((server_script, self._run_prepost_runscript(server, server_script)))
            
            # Execute List of Client Pre Run Scripts if given
            for client_script in self._config.client_prerunscript:
                if not client_script.blocking:
                    self._run_script_on_machine(client, client_script.script)
                else:
                    client_scripts_run.append((client_script, self._run_prepost_runscript(client, client_script)))
            
            # Run Server
            self.logger.info(f'Starting server:\n {server_cmd}\n')
            s = subprocess.Popen(
                server_cmd,
                shell=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE
            )

            time.sleep(2)
            # Run Client
            self.logger.info(f'Starting client:\n {client_cmd}\n')
            testcase._start_time = datetime.now()
            c = subprocess.Popen(
                client_cmd,
                shell=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE
            )
            c_stdout, c_stderr = c.communicate(timeout=testcase.timeout())
            testcase._end_time = datetime.now()
            output = (c_stdout.decode("utf-8") if c_stdout else '') + \
                     (c_stderr.decode("utf-8") if c_stderr else '')

        except subprocess.TimeoutExpired as ex:
            self.logger.error(colored(f"Client expired: {ex}", 'red'))
            expired = True
        finally:
            
            # Execute List of Client Post Run Scripts if given
            for client_script in self._config.client_postrunscript:
                self._run_script_on_machine(
                    host = client,
                    script_path = client_script.script, 
                )

            time.sleep(1)
            
            subprocess.Popen(f'ssh {self._testbed.server.hostname} pkill -f server', shell=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)

            for server_script in self._config.server_postrunscript: # Post run scripts shouldn't be blocking
                self._run_script_on_machine(
                    host = server,
                    script_path = server_script.script, 
                )
            
            self._remove_all_variables_from_machine(
                server
            )

            self._remove_all_variables_from_machine(                    
                client
            )



            if not expired and ('c_stdout' in locals() and 'c_stderr' in locals()): 
                self._log_process(c_stdout, c_stderr, 'Client')
            if 's' in locals():
                s_output = s.communicate()
                self._log_process(*s_output, 'Server')

            for dir in [server_log_dir.name, testcase.certs_dir()]:
                self._pull_directory_from_remote(server, dir)
            for dir in [sim_log_dir.name, client_log_dir.name]:
                self._pull_directory_from_remote(client, dir)

            # TODO calculate timestamps from logs
            if 's' in locals():
                if s.returncode == 127 \
                        or self._is_unsupported(s_output[0].decode("utf-8").splitlines()) \
                        or self._is_unsupported(s_output[1].decode("utf-8").splitlines()):
                    self.logger.error(colored(f"server does not support the test", 'red'))
                    status = TestResult.UNSUPPORTED
                elif not expired:
                    lines = output.splitlines()
                    if c.returncode == 127 or self._is_unsupported(lines):
                        self.logger.error(colored(f"client does not support the test", 'red'))
                        status = TestResult.UNSUPPORTED
                    elif c.returncode == 0 or any("client exited with code 0" in str(line) for line in lines):
                        try:
                            status = testcase.check(client.hostname, server.hostname)
                        except Exception as e:
                            self.logger.error(colored(f"testcase.check() threw Exception: {e}", 'red'))
                            status = TestResult.FAILED
                    else:
                        self.logger.error(colored(f"Client or server failed", 'red'))
                        status = TestResult.FAILED
            else:
                self.logger.error(colored(f"Client or server expired", 'red'))
                status = TestResult.FAILED

            if status == TestResult.SUCCEEDED:
                self.logger.info(colored(f"\u2713 Test successful", 'green'))
            elif status == TestResult.FAILED:
                self.logger.info(colored(f"\u2620 Test failed", 'red'))
            elif status == TestResult.UNSUPPORTED:
                self.logger.info(colored(f"? Test unsupported", 'yellow'))

            # save logs
            self.logger.removeHandler(log_handler)
            log_handler.close()
            if status == TestResult.FAILED or status == TestResult.SUCCEEDED:
                log_dir = self._log_dir + "/" + implementation_name + "_" + implementation_name + "/" + str(testcase)
                if log_dir_prefix:
                    log_dir += "/" + log_dir_prefix
                shutil.copytree(server_log_dir.name, log_dir + "/server")
                shutil.copytree(client_log_dir.name, log_dir + "/client")
                shutil.copytree(sim_log_dir.name, log_dir + "/sim")
                shutil.copyfile(log_file.name, log_dir + "/output.txt")
                if self._save_files and status == TestResult.FAILED:
                    shutil.copytree(testcase.www_dir(), log_dir + "/www")
                    try:
                        shutil.copytree(testcase.download_dir(), log_dir + "/downloads")
                    except Exception as exception:
                        self.logger.info("Could not copy downloaded files: %s", exception)

            if self._testbed:
                self._delete_remote_directory(server, server_log_dir.name)
                self._delete_remote_directory(server, testcase.www_dir())
                self._delete_remote_directory(server, testcase.certs_dir())
                self._delete_remote_directory(client, client_log_dir.name)
                self._delete_remote_directory(client, sim_log_dir.name)
                self._delete_remote_directory(client, testcase.download_dir())

            testcase.cleanup()
            server_log_dir.cleanup()
            client_log_dir.cleanup()
            self.logger.debug("Test took %ss", (datetime.now() - start_time).total_seconds())

            # measurements also have a value
            if hasattr(testcase, "result"):
                value = testcase.result()
            else:
                value = None

            return status, value

    def _setup_env(self, host: Host, path: str) -> None:
        try:
            if host.role in self._prepared_envs:
                return

            venv_command = self._get_venv(host)
            cmd = venv_command + "; ./setup-env.sh"

            cmd = f'ssh {host.hostname} "cd {path}; {cmd}"'

            self.logger.debug(f'Setup:\n {cmd}\n')

            setup_env = subprocess.Popen(
                cmd,
                shell=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE
            )

            self._log_process(*setup_env.communicate(timeout=120), 'setup_env')
            self._prepared_envs.add(host.role)
        except subprocess.TimeoutExpired as ex:
            self.logger.error(colored(f"Setup environment timeout for {host.hostname} ({host.role})", 'red'))
            return ex
        return
    
    def _run_measurement(self, implementation_name: str, server: Host, client: Host, test: Callable[[], TestCase]) -> MeasurementResult:
        values = []
        for i in range(0, test.repetitions()):
            self.logger.info(f"Running repetition {i + 1}/{test.repetitions()}")
            result, value = self._run_testcase(implementation_name, server, client, test, "%d" % (i + 1))
            if result != TestResult.SUCCEEDED:
                if self._continue_on_error:
                    continue
                res = MeasurementResult()
                res.result = result
                res.details = ""
                return res
            values.append(value)

        self.logger.debug(values)
        res = MeasurementResult()
        res.result = TestResult.SUCCEEDED
        res.all_infos = values
        res.details = ""

        if len(values) > 0:
            mean = statistics.mean(values)
            stdev = statistics.stdev(values) if len(values) > 1 else 0
            res.details = "{:.2f} (± {:.2f}) {}".format(
                mean, stdev, test.unit()
            )
        else:
            res.result = TestResult.FAILED
        return res
                
    def _print_results(self) -> None:
        self.logger.info("\n\nRun took %s", datetime.now() - self._start_time)
        for measurement in self.measurement_results.keys():
            table = prettytable.PrettyTable()
            table.title = measurement.name()
            for measurement, result in self.measurement_results[measurement].items():
                table.add_row([measurement, result.details])
            self.logger.info(
                "\n" + 
                str(table)
            )

    def _get_commit_hash(self) -> str:
        p = subprocess.Popen(
            "git rev-parse HEAD",
            shell=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE
        )
        stdout, stderr = p.communicate(timeout=10)
        return stdout.decode("utf-8").rstrip("\n")
    
    def _export_results(self):
        if not self._output:
            self._output = os.path.join(self._log_dir, 'result.json')
            if not os.path.exists(self._log_dir):
                os.makedirs(self._log_dir)

        self.logger.info(f'Exporting results to {self._output}')

        out = {
            "runner_commit_hash": self._get_commit_hash(),
            "runner_start_time_unix_timestamp": self._start_time.timestamp(),
            "runner_end_time_unix_timestamp": datetime.now().timestamp(),
            "log_dir": self._log_dir,
            "implementations": self._config.implementations,
            "build_script": self._config.build_script,
            "tests": {
                x.abbreviation(): {
                    "name": x.name(),
                    "desc": x.desc(),
                }
                for x in self._tests + self._measurements
            },
            "quic_draft": QUIC_DRAFT,
            "quic_version": QUIC_VERSION,
            "results": [],
            "measurements": [],
        }

        measurements = []
        for measurement, implementation_result in self.measurement_results.items():
            for implementation, result in implementation_result.items():
                measurements.append(
                    {
                        "name": measurement.name(),
                        "implementation": implementation,
                        "abbr": measurement.abbreviation(),
                        "filesize": measurement.FILESIZE,
                        "average": result.details,
                        "details": result.all_infos,
                    }
                )  
        out["measurements"].append(measurements)

        f = open(self._output, "w")
        json.dump(out, f)
        f.close()

        # Copy server and client pre- and postscripts into logdir root
        for server_script in self._config.server_prerunscript:
            shutil.copyfile(server_script.script, 
                            os.path.join(self._log_dir, 'spre_' + \
                                         server_script.script.split("/")[1]
                                        )
            )
        for server_script in self._config.server_postrunscript:
            shutil.copyfile(server_script.script, 
                        os.path.join(self._log_dir, 'spost_' + \
                                     server_script.script.split("/")[1]
                                    )
        )
        for client_script in self._config.client_prerunscript:
            shutil.copyfile(client_script.script, 
                            os.path.join(self._log_dir, 'cpre_' + \
                                         client_script.script.split("/")[1]
                                        )
            )
        for client_script in self._config.client_postrunscript:
            shutil.copyfile(client_script.script, 
                        os.path.join(self._log_dir, 'cpost_' + \
                                     client_script.script.split("/")[1]
                                    )
        )
        
    def run(self): 
        self.logger.info(colored(f"Testbed: {self._testbed.basename}", 'white', attrs=['bold']))
        # Copy implementations to hosts
        # self._copy_implementations()
        self._setup_hosts()
        total_tests = len(self._config.implementations) * self._config.repetitions
        finished_tests = 0
        nr_failed = 0

        # run the measurements
        for implementation_name in self._config.implementations:
            for measurement in self._measurements:
                finished_tests += 1

                self.logger.info(
                    colored(
                        "\n---\n"
                        + f"{finished_tests}/{total_tests}\n"
                        + f"Measurement: {measurement.name()}\n"
                        + f"Implementation: {implementation_name}\n"
                        + f"Server: {self._testbed.server.hostname}  "
                        + f"Client: {self._testbed.client.hostname}\n"
                        + "---",
                        'magenta',
                        attrs=['bold']
                    )
                )

                res = self._run_measurement(implementation_name, self._testbed.server, self._testbed.client, measurement)
                self.measurement_results[measurement][implementation_name] = res

        self._print_results()
        self._export_results()
        return nr_failed