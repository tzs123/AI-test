"""文件传输 / 邮件 / 目录服务协议驱动。

stdlib 优先：FTP(ftplib)、SMTP(smtplib)、IMAP(imaplib)、POP3(poplib)。
可选依赖：SFTP(paramiko)、LDAP(ldap3)。凭据用步骤字段 username/password，
不在 URL 中携带（SSRF 校验拒绝带 userinfo 的 URL）。

YAML 步骤示例::

    - id: ftp_list
      action: ftp
      host: 127.0.0.1
      port: 21
      username: anon
      password: anon@
      op: list                 # list / get / put
      path: /
    - id: mail
      action: smtp
      host: 127.0.0.1
      port: 25
      username: ""
      password: ""
      from: a@b.test
      to: c@d.test
      data: "hello"
    - id: ldap
      action: ldap
      url: ldap://127.0.0.1:389
      username: cn=admin,dc=example
      password: secret
      base: dc=example
      filter: "(objectClass=*)"
"""
from __future__ import annotations

import time
from typing import Any, Dict

from backend.url_security import validate_outbound_endpoint, validate_outbound_hostport_url
from .base import (
    attach_runtime_result,
    base_result,
    protocol_sessions,
    register_driver,
    require_dependency,
    resolve,
)
from .base import ProtocolDriver


def _creds(step: Dict[str, Any], context: Any, *, default_user: str = "") -> tuple[str, str]:
    username = str(resolve(context, step.get("username") or default_user) or "")
    password = str(resolve(context, step.get("password") or "") or "")
    return username, password


class FtpDriver(ProtocolDriver):
    name = "ftp"
    actions = {"ftp"}

    def execute(self, step: Dict[str, Any], context: Any) -> Dict[str, Any]:
        import ftplib

        host = str(resolve(context, step.get("host") or ""))
        port = int(resolve(context, step.get("port") or 21) or 21)
        host, port = validate_outbound_endpoint(host, port)
        username, password = _creds(step, context, default_user="anonymous")
        op = str(resolve(context, step.get("op") or "list")).lower()
        path = str(resolve(context, step.get("path") or "/") or "/")
        result = base_result(step, action="ftp")
        result["url"] = f"ftp://{host}:{port}{path}"
        started = time.monotonic()
        try:
            pool = protocol_sessions(context)
            key = ("ftp", host, port)
            ftp = pool.get(key)
            if ftp is None:
                ftp = ftplib.FTP()
                ftp.connect(host, port, timeout=10)
                ftp.login(username, password)
                pool[key] = ftp
            if op == "get":
                chunks: list[bytes] = []
                ftp.retrbinary(f"RETR {path}", chunks.append)
                data = b"".join(chunks)
                result["body"] = data.hex()
                result["text"] = data.decode("utf-8", "replace")
            elif op == "put":
                data = str(resolve(context, step.get("data") or "")).encode("utf-8")
                from io import BytesIO

                ftp.storbinary(f"STOR {path}", BytesIO(data))
            else:
                result["body"] = ftp.nlst(path)
                result["text"] = "\n".join(result["body"] or [])
            result["status"] = "ok"
        except Exception as exc:
            result["status"] = "error"
            result["body"] = str(exc)
            attach_runtime_result(exc, result)
            raise
        finally:
            result["duration_ms"] = round((time.monotonic() - started) * 1000, 2)
        return result


class SftpDriver(ProtocolDriver):
    name = "sftp"
    actions = {"sftp"}
    optional_dependency = "paramiko (pip install paramiko)"

    def execute(self, step: Dict[str, Any], context: Any) -> Dict[str, Any]:
        try:
            import paramiko
        except ImportError:
            require_dependency("paramiko", "pip install paramiko")
        host = str(resolve(context, step.get("host") or ""))
        port = int(resolve(context, step.get("port") or 22) or 22)
        host, port = validate_outbound_endpoint(host, port)
        username, password = _creds(step, context)
        op = str(resolve(context, step.get("op") or "list")).lower()
        path = str(resolve(context, step.get("path") or "/") or "/")
        result = base_result(step, action="sftp")
        result["url"] = f"sftp://{host}:{port}{path}"
        started = time.monotonic()
        try:
            pool = protocol_sessions(context)
            key = ("sftp", host, port)
            client = pool.get(key)
            if client is None:
                transport = paramiko.Transport((host, port))
                transport.connect(username=username or None, password=password or None)
                client = paramiko.SFTPClient.from_transport(transport)
                pool[key] = client
            if op == "get":
                with client.file(path, "rb") as handle:
                    data = handle.read()
                result["body"] = data.hex()
                result["text"] = data.decode("utf-8", "replace")
            elif op == "put":
                data = str(resolve(context, step.get("data") or "")).encode("utf-8")
                with client.file(path, "wb") as handle:
                    handle.write(data)
            else:
                result["body"] = client.listdir(path)
                result["text"] = "\n".join(result["body"] or [])
            result["status"] = "ok"
        except Exception as exc:
            result["status"] = "error"
            result["body"] = str(exc)
            attach_runtime_result(exc, result)
            raise
        finally:
            result["duration_ms"] = round((time.monotonic() - started) * 1000, 2)
        return result


class SmtpDriver(ProtocolDriver):
    name = "smtp"
    actions = {"smtp"}

    def execute(self, step: Dict[str, Any], context: Any) -> Dict[str, Any]:
        import smtplib
        from email.message import EmailMessage

        host = str(resolve(context, step.get("host") or ""))
        port = int(resolve(context, step.get("port") or 25) or 25)
        host, port = validate_outbound_endpoint(host, port)
        username, password = _creds(step, context)
        sender = str(resolve(context, step.get("from") or step.get("sender") or ""))
        recipients = resolve(context, step.get("to") or step.get("recipients") or [])
        if isinstance(recipients, str):
            recipients = [r.strip() for r in recipients.split(",") if r.strip()]
        subject = str(resolve(context, step.get("subject") or ""))
        body = str(resolve(context, step.get("data") or step.get("body") or ""))
        result = base_result(step, action="smtp")
        result["url"] = f"smtp://{host}:{port}"
        started = time.monotonic()
        try:
            msg = EmailMessage()
            msg["From"] = sender
            msg["To"] = ", ".join(recipients)
            msg["Subject"] = subject
            msg.set_content(body)
            pool = protocol_sessions(context)
            key = ("smtp", host, port)
            client = pool.get(key)
            if client is None:
                client = smtplib.SMTP(host, port, timeout=10)
                if username:
                    client.login(username, password)
                pool[key] = client
            client.sendmail(sender, recipients, msg.as_string())
            result["status"] = "ok"
        except Exception as exc:
            result["status"] = "error"
            result["body"] = str(exc)
            attach_runtime_result(exc, result)
            raise
        finally:
            result["duration_ms"] = round((time.monotonic() - started) * 1000, 2)
        return result


class ImapDriver(ProtocolDriver):
    name = "imap"
    actions = {"imap"}

    def execute(self, step: Dict[str, Any], context: Any) -> Dict[str, Any]:
        import imaplib

        host = str(resolve(context, step.get("host") or ""))
        port = int(resolve(context, step.get("port") or 143) or 143)
        host, port = validate_outbound_endpoint(host, port)
        username, password = _creds(step, context)
        op = str(resolve(context, step.get("op") or "search")).lower()
        criteria = str(resolve(context, step.get("filter") or "ALL"))
        result = base_result(step, action="imap")
        result["url"] = f"imap://{host}:{port}"
        started = time.monotonic()
        try:
            pool = protocol_sessions(context)
            key = ("imap", host, port)
            client = pool.get(key)
            if client is None:
                client = imaplib.IMAP4(host, port)
                client.login(username, password)
                pool[key] = client
            client.select("INBOX")
            typ, data = client.search(None, criteria)
            ids = data[0].split() if data and data[0] else []
            result["body"] = [i.decode("ascii") for i in ids]
            if op == "fetch" and ids:
                typ, fetched = client.fetch(ids[-1], "(RFC822)")
                result["text"] = fetched[0][1].decode("utf-8", "replace") if fetched and fetched[0] else ""
            else:
                result["text"] = f"{len(ids)} 封"
            result["status"] = "ok"
        except Exception as exc:
            result["status"] = "error"
            result["body"] = str(exc)
            attach_runtime_result(exc, result)
            raise
        finally:
            result["duration_ms"] = round((time.monotonic() - started) * 1000, 2)
        return result


class Pop3Driver(ProtocolDriver):
    name = "pop3"
    actions = {"pop3"}

    def execute(self, step: Dict[str, Any], context: Any) -> Dict[str, Any]:
        import poplib

        host = str(resolve(context, step.get("host") or ""))
        port = int(resolve(context, step.get("port") or 110) or 110)
        host, port = validate_outbound_endpoint(host, port)
        username, password = _creds(step, context)
        op = str(resolve(context, step.get("op") or "list")).lower()
        result = base_result(step, action="pop3")
        result["url"] = f"pop3://{host}:{port}"
        started = time.monotonic()
        try:
            pool = protocol_sessions(context)
            key = ("pop3", host, port)
            client = pool.get(key)
            if client is None:
                client = poplib.POP3(host, port, timeout=10)
                client.user(username)
                client.pass_(password)
                pool[key] = client
            count, _size = client.stat()
            if op == "retr" and count:
                result["text"] = b"\n".join(client.retr(count)[1]).decode("utf-8", "replace")
            else:
                result["body"] = count
                result["text"] = f"{count} 封"
            result["status"] = "ok"
        except Exception as exc:
            result["status"] = "error"
            result["body"] = str(exc)
            attach_runtime_result(exc, result)
            raise
        finally:
            result["duration_ms"] = round((time.monotonic() - started) * 1000, 2)
        return result


class LdapDriver(ProtocolDriver):
    name = "ldap"
    actions = {"ldap"}
    optional_dependency = "ldap3 (pip install ldap3)"

    def execute(self, step: Dict[str, Any], context: Any) -> Dict[str, Any]:
        try:
            from ldap3 import Connection, Server
        except ImportError:
            require_dependency("ldap3", "pip install ldap3")
        raw_url = str(resolve(context, step.get("url") or ""))
        scheme, host, port, _path, _query = validate_outbound_hostport_url(raw_url)
        username, password = _creds(step, context)
        base = str(resolve(context, step.get("base") or ""))
        flt = str(resolve(context, step.get("filter") or "(objectClass=*)"))
        result = base_result(step, action="ldap")
        result["url"] = raw_url
        started = time.monotonic()
        try:
            server = Server(host, port=port or 389, use_ssl=(scheme == "ldaps"))
            conn = Connection(server, user=username, password=password, auto_bind=True)
            conn.search(search_base=base, search_filter=flt, attributes=["*"])
            result["body"] = [
                {"dn": entry.entry_dn, "attributes": dict(entry.entry_attributes_as_dict)}
                for entry in conn.entries
            ]
            result["text"] = conn.response_to_json() if hasattr(conn, "response_to_json") else ""
            result["status"] = "ok"
            conn.unbind()
        except Exception as exc:
            result["status"] = "error"
            result["body"] = str(exc)
            attach_runtime_result(exc, result)
            raise
        finally:
            result["duration_ms"] = round((time.monotonic() - started) * 1000, 2)
        return result


register_driver(FtpDriver())
register_driver(SftpDriver())
register_driver(SmtpDriver())
register_driver(ImapDriver())
register_driver(Pop3Driver())
register_driver(LdapDriver())
