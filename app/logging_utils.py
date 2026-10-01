import contextvars
import json
import logging

req_id_var = contextvars.ContextVar("request_id", default="-")


class JsonFmt(logging.Formatter):
    def format(self, r):
        d = {"ts": self.formatTime(r), "level": r.levelname, "msg": r.getMessage(), "request_id": req_id_var.get()}
        d.update(getattr(r, "extra_fields", {}))
        return json.dumps(d)


h = logging.StreamHandler()
h.setFormatter(JsonFmt())
log = logging.getLogger("seats")
log.addHandler(h)
log.setLevel(logging.INFO)
log.propagate = False


def L(msg, **kw):
    log.info(msg, extra={"extra_fields": kw})
