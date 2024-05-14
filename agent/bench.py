import json
import os
import shutil
import tempfile
import traceback
from datetime import datetime, timedelta
from glob import glob

import requests

from agent.app import App
from agent.base import AgentException, Base
from agent.job import job, step
from agent.site import Site
from agent.utils import download_file, get_size


class Bench(Base):
    def __init__(self, name, server):
        self.name = name
        self.server = server
        self.directory = os.path.join(self.server.benches_directory, name)
        self.sites_directory = os.path.join(self.directory, "sites")
        self.apps_file = os.path.join(self.directory, "sites", "apps.txt")
        self.bench_config_file = os.path.join(self.directory, "config.json")
        self.config_file = os.path.join(
            self.directory, "sites", "common_site_config.json"
        )
        self.host = self.config.get("db_host", "localhost")
        self.docker_image_name = self.bench_config.get("docker_image_name")
        self.docker_image_tag = self.bench_config.get("docker_image_tag")
        if not (
            os.path.isdir(self.directory)
            and os.path.exists(self.sites_directory)
            and os.path.exists(self.config_file)
            and os.path.exists(self.bench_config_file)
        ):
            raise Exception

    @step("Deploy Bench")
    def deploy(self):
        command = (
            "docker stack deploy --resolve-image=never "
            f"--compose-file docker-compose.yml {self.name} "
        )
        return self.execute(command)

    def dump(self):
        return {
            "name": self.name,
            "apps": {name: app.dump() for name, app in self.apps.items()},
            "config": self.config,
            "sites": {name: site.dump() for name, site in self.sites.items()},
        }

    def fetch_sites_info(self, since=None):
        max_retention_time = (
            datetime.utcnow() - timedelta(days=30)
        ).timestamp()

        if not since:
            since = max_retention_time

        info = {}
        usage_data = []
        log_files = glob(
            os.path.join(
                self.server.directory,
                "logs",
                f"{self.server.name}-usage-*.json.log",
            )
        )
        valid_files = [
            file for file in log_files if os.stat(file).st_mtime > since
        ]

        for file in log_files:
            if (file not in valid_files) and (
                os.stat(file).st_mtime > max_retention_time
            ):
                print(f"Deleting {file}")
                os.remove(file)
            else:
                usage_data.extend(json.load(open(file)))

        for site in self.sites.values():
            try:
                timezone_data = {d.timestamp: d.time_zone for d in usage_data}
                timezone = timezone_data[max(timezone_data)]
            except Exception:
                timezone = None

            info[site.name] = {
                "config": site.config,
                "usage": [
                    {
                        "database": d["database"],
                        "public": d["public"],
                        "private": d["private"],
                        "backups": d["backups"],
                        "timestamp": d["timestamp"],
                    }
                    for d in usage_data
                    if d["site"] == site.name
                ],
                "time_zone": timezone,
            }

        return info

    def execute(self, command, input=None):
        return super().execute(command, directory=self.directory, input=input)

    def docker_execute(self, command, input=None):
        command = (
            f"docker run --rm "
            f"-v {self.sites_directory}:/home/frappe/frappe-bench/sites "
            f"--net {self.name}_default "
            f"-it {self.docker_image_name}:{self.docker_image_tag} {command}"
        )
        return self.execute(command, input=input)

    @step("New Site")
    def bench_new_site(self, name, mariadb_root_password, admin_password):
        return self.docker_execute(
            "bench new-site "
            f"--admin-password {admin_password} "
            f"--no-mariadb-socket "
            f"--mariadb-root-password {mariadb_root_password} "
            f"{name}"
        )

    def fetch_monitor_data(self):
        lines = []
        try:
            monitor_log_file = os.path.join(
                self.directory, "logs", "monitor.json.log"
            )
            time = datetime.utcnow().isoformat()
            logs_directory = os.path.join(
                self.server.directory,
                "logs",
            )
            target_file = os.path.join(
                logs_directory,
                f"{self.name}-{time}-monitor.json.log",
            )
            if os.path.exists(monitor_log_file):
                shutil.move(monitor_log_file, target_file)

                with open(target_file) as f:
                    for line in f.readlines():
                        try:
                            lines.append(json.loads(line))
                        except Exception:
                            traceback.print_exc()

            now = datetime.now().timestamp()
            for file in os.listdir(logs_directory):
                path = os.path.join(logs_directory, file)
                if file.endswith("-monitor.json.log") and (
                    now - os.stat(path).st_mtime
                ) > (7 * 86400):
                    os.remove(path)
        except FileNotFoundError:
            pass
        except Exception:
            traceback.print_exc()
        return lines

    def status(self):
        def _touch_currentsite_file(bench):
            file = os.path.join(bench.sites_directory, "currentsite.txt")
            open(file, "w").close()

        def _inactive_scheduler_sites(bench):
            inactive = []
            _touch_currentsite_file(bench)
            try:
                doctor = bench.docker_execute("bench doctor")["output"].split(
                    "\n"
                )
            except AgentException as e:
                doctor = e.data["output"]

            for line in doctor:
                if "inactive" in line:
                    site = line.split(" ")[-1]
                    inactive.append(site)
            return inactive

        def _inactive_web_sites(bench):
            inactive = []
            session = requests.Session()
            for site in bench.sites.keys():
                url = f"https://{site}/api/method/ping"
                try:
                    result = session.get(url)
                except Exception as e:
                    result = None
                    print("Ping Failed", url, e)
                if not result or result.status_code != 200:
                    inactive.append(site)
            return inactive

        status = {
            "sites": {
                site: {"scheduler": True, "web": True}
                for site in self.sites.keys()
            },
            "timestamp": str(datetime.now()),
        }

        for site in _inactive_scheduler_sites(self):
            status["sites"][site]["scheduler"] = False

        for site in _inactive_web_sites(self):
            status["sites"][site]["web"] = False

        return status

    @job("New Site", priority="high")
    def new_site(
        self, name, config, apps, mariadb_root_password, admin_password
    ):
        self.bench_new_site(name, mariadb_root_password, admin_password)
        site = Site(name, self)
        site.install_apps(apps)
        site.update_config(config)
        self.setup_nginx()
        self.server.reload_nginx()

    @job("New Site from Backup", priority="high")
    def new_site_from_backup(
        self,
        name,
        default_config,
        apps,
        mariadb_root_password,
        admin_password,
        site_config,
        database,
        public,
        private,
    ):
        files = self.download_files(name, database, public, private)
        self.bench_new_site(name, mariadb_root_password, admin_password)
        site = Site(name, self)
        site.update_config(default_config)
        try:
            site.restore(
                mariadb_root_password,
                admin_password,
                files["database"],
                files["public"],
                files["private"],
            )
            if site_config:
                site_config = json.loads(site_config)
                site.update_config(site_config)
        finally:
            self.delete_downloaded_files(files["database"])
        site.uninstall_unavailable_apps(apps)
        site.migrate()
        site.set_admin_password(admin_password)
        site.enable_scheduler()
        self.setup_nginx()
        self.server.reload_nginx()

        return site.bench_execute("list-apps")

    @step("Archive Site")
    def bench_archive_site(self, name, mariadb_root_password):
        return self.docker_execute(
            f"bench drop-site {name} "
            f"--root-password {mariadb_root_password} --no-backup "
            "--archived-sites-path archived"
        )

    @step("Download Backup Files")
    def download_files(self, name, database_url, public_url, private_url):
        folder = tempfile.mkdtemp(prefix="agent-upload-", suffix=f"-{name}")
        database_file = download_file(database_url, prefix=folder)
        private_file = download_file(private_url, prefix=folder)
        public_file = download_file(public_url, prefix=folder)
        return {
            "database": database_file,
            "private": private_file,
            "public": public_file,
        }

    @step("Delete Downloaded Backup Files")
    def delete_downloaded_files(self, database_file):
        shutil.rmtree(os.path.dirname(database_file))

    @job("Archive Site")
    def archive_site(self, name, mariadb_root_password):
        self.bench_archive_site(name, mariadb_root_password)
        self.setup_nginx()
        self.server.reload_nginx()

    @step("Bench Setup NGINX")
    def setup_nginx(self):
        return self.execute("bench setup nginx --yes")

    @step("Bench Setup NGINX Target")
    def setup_nginx_target(self):
        return self.execute("bench setup nginx --yes")

    @step("Bench Disable Production")
    def disable_production(self):
        return self.execute(f"docker stack rm {self.name}")

    @property
    def apps(self):
        with open(self.apps_file, "r") as f:
            apps_list = f.read().split("\n")

        apps = {}
        for directory in apps_list:
            try:
                apps[directory] = App(directory, self)
            except Exception:
                pass
        return apps

    @step("Update Bench Configuration")
    def update_config(self, value):
        new_config = self.config
        new_config.update(value)
        self.setconfig(new_config)

    @job("Update Bench Configuration", priority="high")
    def update_config_job(self, value):
        self.update_config(value)
        self.setup_supervisor()
        self.server.update_supervisor()
        self.setup_nginx()
        self.server.reload_nginx()

    @property
    def job_record(self):
        return self.server.job_record

    @property
    def sites(self):
        sites = {}
        for directory in os.listdir(self.sites_directory):
            try:
                sites[directory] = Site(directory, self)
            except Exception:
                pass
        return sites

    @property
    def step_record(self):
        return self.server.step_record

    def get_usage(self):
        return {
            "storage": get_size(self.directory),
            "database": sum(
                [site.get_database_size() for site in self.sites.values()]
            ),
        }

    @property
    def bench_config(self):
        with open(self.bench_config_file, "r") as f:
            return json.load(f)

    def set_bench_config(self, value, indent=1):
        with open(self.config_file, "w") as f:
            json.dump(value, f, indent=indent, sort_keys=True)
