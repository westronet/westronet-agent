import json
import os
import subprocess
import traceback
from datetime import datetime
from functools import partial


class Base:
    def __init__(self):
        self.directory = None
        self.config_file = None
        self.name = None

    def __repr__(self):
        return f"{self.__class__.__name__}({self.name})"

    def execute(
        self,
        command,
        directory=None,
        input=None,
        skip_output_log=False,
        executable=None,
        remove_crs=True,
    ):
        directory = directory or self.directory
        self.log("Command", command)
        self.log("Directory", directory)
        start = datetime.now()
        data = {"command": command, "directory": directory, "start": start}
        try:
            output = self.run_subprocess(command, directory, input, executable)
        except subprocess.CalledProcessError as e:
            end = datetime.now()
            data.update({"duration": end - start, "end": end})
            output = (
                self.remove_crs(e.output)
                if remove_crs
                else e.output.decode().strip()
            )
            if not skip_output_log:
                self.log("Output", output)
            data.update(
                {
                    "output": output,
                    "returncode": e.returncode,
                    "traceback": "".join(traceback.format_exc()),
                }
            )
            raise AgentException(data)

        end = datetime.now()
        output = (
            self.remove_crs(output) if remove_crs else output.decode().strip()
        )
        if not skip_output_log:
            self.log("Output", output)
        data.update({"duration": end - start, "end": end, "output": output})
        return data

    def run_subprocess(self, command, directory, input, executable):
        # Start a child process and start reading output immediately
        with subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            stdin=subprocess.PIPE if input else None,
            cwd=directory,
            shell=True,
            executable=executable,
        ) as process:
            if input:
                process._stdin_write(input.encode())

            output = self.parse_output(process)
            retcode = process.poll()
            # This is equivalent of check=True
            # Raise an exception if the process returns a non-zero return code
            if retcode:
                raise subprocess.CalledProcessError(
                    retcode, command, output=output
                )
        return output

    def parse_output(self, process):
        lines = []
        # This is equivalent of remove_crs
        # Make sure output matches what'll be shown in the terminal
        # This won't work for top, htop etc, but good enough to handle progress bars
        if process.stdout:
            line = ""
            for char in iter(partial(process.stdout.read, 1), ""):
                char = char.decode()
                if char == "" and process.poll() is not None:
                    break
                elif char == "\r":
                    # Overwrite last line
                    if lines:
                        lines[-1] = line
                    line = ""
                elif char == "\n":
                    lines.append(line)
                    line = ""
                else:
                    line += char
        return "\n".join(lines)

    @property
    def config(self):
        with open(self.config_file, "r") as f:
            return json.load(f)

    def setconfig(self, value, indent=1):
        with open(self.config_file, "w") as f:
            json.dump(value, f, indent=indent, sort_keys=True)

    def remove_crs(self, input):
        output = subprocess.check_output(["col", "-b"], input=input)
        return output.decode().strip()

    def log(self, *args):
        print(*args)

    @property
    def logs(self):
        def path(file):
            return os.path.join(self.logs_directory, file)

        def modified_time(file):
            return os.path.getctime(path(file))

        try:
            log_files = sorted(
                os.listdir(self.logs_directory),
                key=modified_time,
                reverse=True,
            )
            payload = []

            for x in log_files:
                stats = os.stat(path(x))
                payload.append(
                    {
                        "name": x,
                        "size": stats.st_size / 1000,
                        "created": str(datetime.fromtimestamp(stats.st_ctime)),
                        "modified": str(
                            datetime.fromtimestamp(stats.st_mtime)
                        ),
                    }
                )

            return payload

        except FileNotFoundError:
            return []

    def retrieve_log(self, name):
        if name not in {x["name"] for x in self.logs}:
            return ""
        log_file = os.path.join(self.logs_directory, name)
        return open(log_file).read()


class AgentException(Exception):
    def __init__(self, data):
        self.data = data
