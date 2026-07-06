import os
import pathlib
import subprocess

from store.deployment.subdomain_manager import SubdomainManager


class NginxConfigError(Exception):
    pass


class LocalCommandRunner:
    def run(self, argv, *, timeout=60, input=None):
        return subprocess.run(argv, check=False, capture_output=True, text=True, timeout=timeout, input=input)


class NginxConfigGenerator:
    available_root = pathlib.Path("/etc/nginx/sites-available/qasedak")
    enabled_root = pathlib.Path("/etc/nginx/sites-enabled/qasedak")

    def __init__(self, *, available_root=None, enabled_root=None, command_runner=None):
        self.available_root = pathlib.Path(
            available_root or os.environ.get("QASEDAK_NGINX_AVAILABLE_ROOT") or self.available_root
        )
        self.enabled_root = pathlib.Path(
            enabled_root or os.environ.get("QASEDAK_NGINX_ENABLED_ROOT") or self.enabled_root
        )
        self.command_runner = command_runner or LocalCommandRunner()
        self.subdomains = SubdomainManager()

    def config_filename(self, tenant_id):
        tenant = self.subdomains.validate_tenant_id(tenant_id)
        return f"qasedak_{tenant}.conf"

    def config_path(self, tenant_id):
        return self._safe_child(self.available_root, self.config_filename(tenant_id))

    def enabled_path(self, tenant_id):
        return self._safe_child(self.enabled_root, self.config_filename(tenant_id))

    def generate(self, *, tenant_id, subdomain, port):
        subdomain = self.subdomains.validate_subdomain(subdomain)
        port = int(port)
        if port < 8001 or port > 65535:
            raise NginxConfigError("Invalid upstream port.")
        self.subdomains.validate_tenant_id(tenant_id)
        return (
            "server {\n"
            f"    server_name {subdomain};\n\n"
            "    location / {\n"
            f"        proxy_pass http://127.0.0.1:{port};\n"
            "        proxy_set_header Host $host;\n"
            "        proxy_set_header X-Real-IP $remote_addr;\n"
            "        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;\n"
            "        proxy_set_header X-Forwarded-Proto $scheme;\n"
            "    }\n"
            "}\n"
        )

    def write_config(self, *, tenant_id, subdomain, port):
        path = self.config_path(tenant_id)
        path.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
        content = self.generate(tenant_id=tenant_id, subdomain=subdomain, port=port)
        existing = path.read_text(encoding="utf-8") if path.exists() else ""
        if existing and "server_name " not in existing:
            raise NginxConfigError("Refusing to overwrite unmanaged Nginx config.")
        path.write_text(content, encoding="utf-8")
        os.chmod(path, 0o644)
        return str(path)

    def enable_site(self, tenant_id):
        source = self.config_path(tenant_id)
        target = self.enabled_path(tenant_id)
        target.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
        if target.is_symlink() or target.exists():
            if target.is_symlink() and pathlib.Path(os.readlink(target)) == source:
                return str(target)
            raise NginxConfigError("Refusing to overwrite existing enabled site.")
        target.symlink_to(source)
        return str(target)

    def reload(self):
        test = self.command_runner.run(["nginx", "-t"], timeout=60)
        if test.returncode != 0:
            raise NginxConfigError((test.stderr or "nginx -t failed").strip())
        reload_result = self.command_runner.run(["systemctl", "reload", "nginx"], timeout=60)
        if reload_result.returncode == 0:
            return
        fallback = self.command_runner.run(["nginx", "-s", "reload"], timeout=60)
        if fallback.returncode != 0:
            raise NginxConfigError((fallback.stderr or reload_result.stderr or "nginx reload failed").strip())

    def remove(self, tenant_id):
        for path in (self.enabled_path(tenant_id), self.config_path(tenant_id)):
            if path.is_symlink() or path.exists():
                path.unlink()

    def _safe_child(self, root, filename):
        root = pathlib.Path(root)
        if pathlib.PurePath(filename).name != filename:
            raise NginxConfigError("Unsafe Nginx config filename.")
        path = root / filename
        try:
            path.parent.resolve().relative_to(root.resolve())
        except ValueError as exc:
            raise NginxConfigError("Unsafe Nginx config path.") from exc
        return path
