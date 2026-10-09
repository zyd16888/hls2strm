"""中转会话：每个播放清单固定源、线路和直链快照，互不切换。"""

from __future__ import annotations

import secrets
import json
import time
from collections import OrderedDict

from .errors import NotFound


class PlaybackSessions:
    def __init__(self, capacity: int = 2048):
        self.capacity = capacity
        self.items = OrderedDict()
        self.created = 0

    def create(self, resolved):
        key = secrets.token_hex(12)
        ttl = max(8 * 3600, (resolved.video.get("duration") or 0) + 3600)
        self.items[key] = (time.monotonic() + ttl, resolved)
        while len(self.items) > self.capacity:
            self.items.popitem(last=False)
        return key

    def get(self, key: str, source_id: int):
        item = self.items.get(key)
        if item is None or item[0] <= time.monotonic():
            self.items.pop(key, None)
            raise NotFound("播放会话已失效，请重新打开播放清单")
        if item[1].source["id"] != source_id:
            raise NotFound("播放会话与源不匹配")
        self.items.move_to_end(key)
        return item[1]

    def update(self, key: str, resolved):
        if key in self.items:
            self.items[key] = (self.items[key][0], resolved)

    async def persist(self, db, resolved):
        key = self.create(resolved)
        ttl = self.items[key][0] - time.monotonic()
        data = {"video": resolved.video, "source": resolved.source, "line": resolved.line,
                "proxy_forced": resolved.proxy_forced}
        await db._write("INSERT INTO play_sessions(id,source_id,data,expires_at) VALUES(?,?,?,?)",
                        (key, resolved.source["id"], json.dumps(data), int(time.time()+ttl)))
        self.created += 1
        if self.created % 64 == 0:
            await db._write("DELETE FROM play_sessions WHERE expires_at<=?", (int(time.time()),))
        return key

    async def restore(self, db, key: str, source_id: int):
        try:
            return self.get(key, source_id)
        except NotFound:
            row = await db._one("SELECT data,expires_at FROM play_sessions WHERE id=? AND source_id=? AND expires_at>?",
                                (key, source_id, int(time.time())))
            if row is None:
                raise NotFound("播放会话已失效，请重新打开播放清单") from None
            from .play import Resolved
            from .sites import get_site
            data = json.loads(row["data"])
            resolved = Resolved(data["video"], data["source"], get_site(data["source"]["site"]), data["line"], data["proxy_forced"])
            self.items[key] = (time.monotonic()+row["expires_at"]-time.time(), resolved)
            while len(self.items) > self.capacity:
                self.items.popitem(last=False)
            return resolved
