import os
import smtplib
from email.message import EmailMessage
from email.utils import formataddr, parseaddr
from typing import Any, Iterable, List, Optional, Sequence, Tuple


class EmailDeliveryError(Exception):
    """Wraps any delivery failure so the API can respond with 5xx."""


Attachment = Tuple[str, bytes, str]
InlineAttachment = Tuple[str, bytes, str, str]


def _env_bool(var_name: str, default: bool) -> bool:
    value = os.getenv(var_name)
    if value is None:
        return default
    return value.lower() not in {"0", "false", "no"}


def _get_config_value(source: Optional[Any], key: str):
    if source is None:
        return None
    if isinstance(source, dict):
        return source.get(key)
    return getattr(source, key, None)


def send_email(
    *,
    recipients: Sequence[str],
    subject: str,
    html_body: str,
    cc: Optional[Iterable[str]] = None,
    text_body: Optional[str] = None,
    attachments: Optional[List[Attachment]] = None,
    inline_attachments: Optional[List[InlineAttachment]] = None,
    smtp_config: Optional[Any] = None,
) -> None:
    """
    Sends an email using simple SMTP credentials defined via env variables.

    Attachments must be provided as tuples (filename, bytes_content, mimetype).
    """
    recipient_list = [addr for addr in recipients if addr]
    if not recipient_list:
        raise ValueError("Debe proporcionar al menos un destinatario")

    smtp_host = _get_config_value(smtp_config, "smtp_host") or os.getenv("SMTP_HOST")
    email_from = _get_config_value(smtp_config, "email_from") or os.getenv("EMAIL_FROM")
    email_from_name = (
        _get_config_value(smtp_config, "email_from_name")
        or _get_config_value(smtp_config, "company_name")
        or os.getenv("EMAIL_FROM_NAME")
    )
    config_port = _get_config_value(smtp_config, "smtp_port")
    smtp_port = int(config_port) if config_port else int(os.getenv("SMTP_PORT", "587"))
    smtp_user = _get_config_value(smtp_config, "smtp_user") or os.getenv("SMTP_USER")
    smtp_password = (
        _get_config_value(smtp_config, "smtp_password") or os.getenv("SMTP_PASSWORD")
    )
    use_tls_config = _get_config_value(smtp_config, "smtp_use_tls")
    use_tls = (
        bool(use_tls_config)
        if use_tls_config is not None
        else _env_bool("SMTP_USE_TLS", True)
    )

    if not smtp_host or not email_from:
        raise ValueError("Configura SMTP en /pos/settings antes de enviar correos")

    cc_list = [addr for addr in (cc or []) if addr]
    all_recipients = list(recipient_list) + cc_list

    parsed_name, parsed_email = parseaddr(str(email_from))
    sender_email = (parsed_email or str(email_from or "")).strip()
    sender_name = (str(email_from_name or "").strip() or parsed_name.strip())
    from_header = formataddr((sender_name, sender_email)) if sender_name else sender_email

    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = from_header
    message["To"] = ", ".join(recipient_list)
    if cc_list:
        message["Cc"] = ", ".join(cc_list)

    if not text_body:
        text_body = "Este correo contiene información del POS en formato HTML."
    message.set_content(text_body)
    message.add_alternative(html_body, subtype="html")
    html_part = message.get_body(preferencelist=("html",))

    if html_part:
        for inline_attachment in inline_attachments or []:
            filename, content, mimetype, content_id = inline_attachment
            maintype, subtype = mimetype.split("/", 1)
            safe_content_id = content_id.strip().strip("<>")
            if not safe_content_id:
                continue
            html_part.add_related(
                content,
                maintype=maintype,
                subtype=subtype,
                cid=f"<{safe_content_id}>",
                filename=filename,
                disposition="inline",
            )

    for attachment in attachments or []:
        filename, content, mimetype = attachment
        maintype, subtype = mimetype.split("/", 1)
        message.add_attachment(
            content,
            maintype=maintype,
            subtype=subtype,
            filename=filename,
        )

    try:
        server = smtplib.SMTP(smtp_host, smtp_port)
        if use_tls:
            server.starttls()

        with server:
            if smtp_user and smtp_password:
                server.login(smtp_user, smtp_password)
            server.sendmail(sender_email, all_recipients, message.as_string())
    except Exception as exc:  # pragma: no cover - actual network errors
        raise EmailDeliveryError(str(exc)) from exc
