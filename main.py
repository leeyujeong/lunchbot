#!/usr/bin/env python
# coding: utf-8

import collections
import datetime
import json
import re
import time

import Skype4Py

from storage import menu, order_record
try:
    import settings
except ImportError:
    settings = object()


class CappedSet(set):
    def __init__(self, maxlen):
        super(CappedSet, self).__init__()
        self.maxlen = maxlen
        self.q = collections.deque(maxlen=maxlen)
    def add(self, x):
        if x in self:
            return
        if len(self) >= self.maxlen:
            old = self.q.popleft()
            self.discard(old)
        super(CappedSet, self).add(x)
        self.q.append(x)

handle2fullname = {}

class Order(object):
    def __init__(self):
        self.clear()
    def add(self, name, price, qty=1):
        self.menus[name] += qty
        self.total += price * qty
    def clear(self):
        self.menus = collections.Counter()
        self.total = 0
    def populate(self, menus):
        self.menus = collections.Counter(menus)
        self.total = sum(
            menu.get(name)[1] * qty for name, qty in menus.items()
        )
        return self
    def copy(self):
        c = Order()
        c.menus = self.menus.copy()
        c.total = self.total
        return c
    def summary(self):
        return u'{} = {:,}'.format(
            u' + '.join(
                u'{} x {}'.format(name, qty)
                for name, qty in self.menus.items()
            ),
            self.total
        )

orders = collections.defaultdict(Order)

class LunchOrderBot(object):
    cmd_pattern = re.compile(ur'^!([a-z_\d]+)\b', flags=re.IGNORECASE)
    qty_pattern = re.compile(ur'^(?P<name>.*)\s*[x*]\s*(?P<qty>\d+)\s*$',
                             flags=re.IGNORECASE | re.UNICODE)
    sep_pattern = re.compile(ur'[.,;+/]|\band\b', flags=re.IGNORECASE | re.UNICODE)

    def __init__(self, sqlite_path, channels):
        self.sqlite_path = sqlite_path
        self.channels = set(channels)
        self.last_orderer = None
        self.seen = CappedSet(maxlen=1024)
        self.skype = Skype4Py.Skype(Events=self)
        self.skype.FriendlyName = "Skype Bot"
        self.skype.Attach()

    def MessageStatus(self, msg, status):
        if msg.ChatName not in self.channels:
            if msg.Body not in ('!summon', '!whereami'):
                return
        handle2fullname[msg.Sender.Handle] = msg.Sender.FullName
        if status in (Skype4Py.cmsReceived, Skype4Py.cmsSent, Skype4Py.cmsSending):
            if status == Skype4Py.cmsReceived:
                msg.MarkAsSeen()
            elif msg.Id in self.seen:
                return

            self.handle_order(msg) or self.handle_misc(msg)
            self.seen.add(msg.Id)

    def send_text(self, msg, text):
        sent = msg.Chat.SendMessage(text)
        self.seen.add(sent.Id)

    def handle_order(self, msg):
        any_order = False
        for item in self.sep_pattern.split(msg.Body):
            matched = self.qty_pattern.match(item.strip())
            if matched:
                name, qty = matched.group('name'), int(matched.group('qty'))
            else:
                name, qty = item, 1
            name_price = menu.get(name.replace(u' ', u''))
            if not name_price:
                continue
            any_order = True
            name, price = name_price
            o = orders[msg.Sender.Handle]
            o.add(*name_price, qty=qty)
        if any_order:
            self.send_text(msg, o.summary())
            self.last_orderer = msg.FromHandle
        return any_order

    def _handle_metoo(self, msg):
        if self.last_orderer not in orders:
            return
        o = orders[msg.FromHandle] = orders[self.last_orderer].copy()
        self.send_text(msg, o.summary())

    def handle_misc(self, msg):
        matched = self.cmd_pattern.match(msg.Body.strip())
        if not matched:
            return
        cmd = matched.group(1)
        attr = getattr(self, '_handle_{}'.format(cmd), None)
        if callable(attr):
            attr(msg)

    def _handle_hello(self, msg):
        self.send_text(
            msg,
            u'점심봇 (experimental): '
            u'한솥 도시락을 드실분은 알려주세요. '
            u'현민님이 주문 대행해 드립니다.\n'
            u'http://www.hsd.co.kr/lunch/lunchList.html\n'
            u'봇은 거들뿐... ' +
            u', '.join('!{}'.format(
                x.split('_', 2)[-1])
                for x in dir(self) if x.startswith('_handle')
            )
        )

    def _handle_clear(self, msg):
        orders.pop(msg.Sender.Handle, None)
        self.send_text(msg, u'{0.FullName} ({0.Handle}): OUT'.format(msg.Sender))
    def _handle_clearall(self, msg):
        orders.clear()
        self.send_text(msg, u'EMPTY')
    def _handle_sum(self, msg):
        if not orders:
            self.send_text(msg, u'읭? No order at all.')
            return
        text = []
        text.append(u' Menu '.center(80, u'-'))
        cnt = collections.Counter()
        for o in orders.values(): cnt += o.menus
        for name, c in cnt.most_common():
            text.append(u'{} x {}'.format(name, c))
        text.append(u' Show me the money '.center(80, u'-'))
        for handle, o in orders.items():
            text.append(u'{} ({}): {}'.format(
                handle2fullname[handle],
                handle,
                o.summary()
            ))
        text.append(
            u' Total: {:,} '.format(
                sum(o.total for o in orders.values())
            ).center(80, u'-')
        )
        self.send_text(msg, u'\n'.join(text))
    def _handle_menu(self, msg):
        self.send_text(
            msg,
            u'\n'.join(
                u'{} - {:,}'.format(name, price)
                for name, price in menu.getall()
            )
        )
    def _handle_fin(self, msg):
        timestamp = time.time()
        for handle, o in orders.items():
            order_record.add(
                handle, handle2fullname[handle], dict(o.menus), o.total, timestamp
            )
        self.send_text(msg, u'주문 들어갑니다.')

    def _handle_ping(self, msg):
        self.send_text(msg, u'pong')
    def _handle_summon(self, msg):
        self.channels.add(msg.ChatName)
    def _handle_dismiss(self, msg):
        self.channels.remove(msg.ChatName)
    def _handle_whereami(self, msg):
        self.send_text(msg, msg.ChatName)
    def _handle_salt(self, msg):
        "Same as last time"
        try:
            offset = int(msg.Body.split()[1])
        except (IndexError, ValueError):
            offset = 0
        o = order_record.get_last_order(msg.FromHandle, offset)
        if not o:
            self.send_text(msg, u'No order')
            return
        items = json.loads(o)
        self.send_text(msg, orders[msg.FromHandle].populate(items).summary())
    def _handle_recent_orders(self, msg):
        records = order_record.get_recent_orders(msg.FromHandle)
        def _():
            for i, (items, total, timestamp) in enumerate(records):
                dt = datetime.datetime.fromtimestamp(timestamp)
                items = json.loads(items)
                yield u'{}. {:%Y-%m-%d} : {} = {:,}'.format(
                    i, dt,
                    u' + '.join(u'{} x {}'.format(name, cnt) for name, cnt in items.items()),
                    total
                )
        txt = u'\n'.join(reversed(list(_())))
        self.send_text(msg, txt or u'No order')


if __name__ == "__main__":
    bot = LunchOrderBot(
        'lunch.sqlite',
        channels=getattr(settings, 'channels', [])
    )
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        pass
