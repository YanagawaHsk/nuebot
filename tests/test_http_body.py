"""Synthetic rejected-body framing and deadlines; no real credentials or API."""
import io
import sys
import unittest
from email.message import Message
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import http_body


class Connection:
    def __init__(self,timeout=None):self.timeout=timeout;self.calls=[]
    def gettimeout(self):return self.timeout
    def settimeout(self,value):self.calls.append(value);self.timeout=value


class Reader:
    def __init__(self,data):self.data=io.BytesIO(data);self.calls=[]
    def read1(self,length):self.calls.append(length);return self.data.read(length)
    def read(self,*_args):raise AssertionError('Rejected bodies must use one bounded raw read at a time')


def handler(data=b'data',headers=None,command='POST',timeout=None):
    message=Message()
    for key,value in (headers or [('Content-Length',str(len(data)))]):message[key]=value
    return SimpleNamespace(command=command,headers=message,rfile=Reader(data),connection=Connection(timeout),close_connection=False)


class RejectedPostBodyTests(unittest.TestCase):
    def test_valid_small_body_is_discarded_in_chunks_without_parsing_or_tail_read(self):
        h=handler(b'x'*150000+b'UNREAD_TAIL',[('Content-Length','150000')],timeout=7)
        self.assertTrue(http_body.drain_rejected_post(h))
        self.assertTrue(h.close_connection)
        self.assertTrue(h._request_body_consumed)
        self.assertEqual(h.rfile.data.read(),b'UNREAD_TAIL')
        self.assertLessEqual(max(h.rfile.calls),8192)
        self.assertEqual(sum(h.rfile.calls),150000)
        self.assertEqual(h.connection.gettimeout(),7)
        self.assertLessEqual(max(v for v in h.connection.calls if v!=7),.25)

    def test_header_ambiguity_expect_transfer_and_oversize_never_read_or_change_timeout(self):
        for headers in ([],[('Content-Length','4'),('Content-Length','4')],
            [('Content-Length','4,4')],[('Content-Length','-1')],
            [('Content-Length','+4')],[('Content-Length','4.0')],
            [('Content-Length','４')],[('Content-Length','NaN')],
            [('Content-Length','150001')],[('Content-Length','9'*5000)],
            [('Content-Length','4'),('Transfer-Encoding','chunked')],
            [('Content-Length','4'),('Transfer-Encoding','')],
            [('Content-Length','4'),('Expect','100-continue')],
            [('Content-Length','4'),('Expect','')]):
            with self.subTest(headers=headers):
                h=handler();h.headers=Message()
                for k,v in headers:h.headers[k]=v
                self.assertFalse(http_body.drain_rejected_post(h))
                self.assertTrue(h.close_connection)
                self.assertEqual(h.rfile.calls,[])
                self.assertEqual(h.connection.calls,[])

    def test_zero_and_ows_leading_zeros_are_fixed_length_without_extra_read(self):
        for value in ('0','0000'):
            h=handler(b'extra',[('Content-Length',value)])
            self.assertTrue(http_body.drain_rejected_post(h))
            self.assertEqual(h.rfile.calls,[])
        h=handler(b'data',[('Content-Length',' \t0000000004\t ')])
        self.assertTrue(http_body.drain_rejected_post(h))
        self.assertEqual(h.rfile.calls,[4])

    def test_slow_trickle_obeys_one_total_deadline_and_restores_original_timeout(self):
        clock=[0.0]
        h=handler(b'x'*100,timeout=9)
        def trickle(length):
            delay=min(.09,h.connection.timeout)
            clock[0]+=delay
            if delay<.09:raise TimeoutError('synthetic timeout')
            return b'x'
        h.rfile.read1=trickle
        with patch.object(http_body.time,'monotonic',side_effect=lambda:clock[0]):
            self.assertFalse(http_body.drain_rejected_post(h))
        self.assertAlmostEqual(clock[0],.25)
        self.assertEqual(h.connection.gettimeout(),9)
        waits=h.connection.calls[:-1]
        self.assertEqual(len(waits),3)
        self.assertGreater(waits[0],waits[1]);self.assertGreater(waits[1],waits[2])

    def test_eof_disconnect_and_repeat_never_grant_a_second_read_budget(self):
        h=handler(b'x',[('Content-Length','4')],timeout=3)
        self.assertFalse(http_body.drain_rejected_post(h))
        calls=list(h.rfile.calls)
        self.assertFalse(http_body.drain_rejected_post(h))
        self.assertEqual(h.rfile.calls,calls)
        self.assertEqual(h.connection.gettimeout(),3)
        h=handler(timeout=2)
        h.rfile.read1=lambda _size:(_ for _ in ()).throw(ConnectionAbortedError('synthetic disconnect'))
        self.assertFalse(http_body.drain_rejected_post(h))
        self.assertEqual(h.connection.gettimeout(),2)

    def test_get_and_already_consumed_post_never_touch_body(self):
        for command in ('GET','HEAD','PUT','PATCH','DELETE'):
            h=handler(command=command)
            self.assertFalse(http_body.drain_rejected_post(h))
            self.assertEqual(h.rfile.calls,[])
            self.assertFalse(h.close_connection)
        h=handler();h._request_body_consumed=True
        self.assertTrue(http_body.drain_rejected_post(h))
        self.assertEqual(h.rfile.calls,[])
        self.assertTrue(h.close_connection)

    def test_unbuffered_reader_is_not_replaced_with_a_potentially_blocking_read(self):
        h=handler();h.rfile=SimpleNamespace(read=lambda _size:self.fail('read fallback is unsafe'))
        self.assertFalse(http_body.drain_rejected_post(h))
        self.assertEqual(h.connection.calls,[])


if __name__=='__main__':unittest.main()
