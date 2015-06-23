import json

import mock
from twisted.internet import reactor, task
from twisted.trial import unittest

from txrudp import connection, constants, packet, rudp


class TestScheduledPacketAPI(unittest.TestCase):

    """Test API (attributes) of ScheduledPacket subclass."""

    @classmethod
    def setUpClass(cls):
        cls.spclass = connection.Connection.ScheduledPacket

    def test_default_init(self):
        rudp_packet = json.dumps(
            packet.Packet(
                1,
                ('123.45.67.89', 12345),
                ('213.54.76.98', 54321)
            ).to_json()
        )
        timeout = 0.7
        timeout_cb = reactor.callLater(timeout, lambda: None)
        sp = self.spclass(rudp_packet, timeout, timeout_cb)

        self.assertEqual(sp.rudp_packet, rudp_packet)
        self.assertEqual(sp.timeout, timeout)
        self.assertEqual(sp.timeout_cb, timeout_cb)
        self.assertEqual(sp.retries, 0)

        timeout_cb.cancel()

    def test_init_with_retries(self):
        rudp_packet = json.dumps(
            packet.Packet(
                1,
                ('123.45.67.89', 12345),
                ('213.54.76.98', 54321)
            ).to_json()
        )
        timeout = 0.7
        timeout_cb = reactor.callLater(timeout, lambda: None)
        sp = self.spclass(rudp_packet, timeout, timeout_cb, retries=10)

        self.assertEqual(sp.rudp_packet, rudp_packet)
        self.assertEqual(sp.timeout, timeout)
        self.assertEqual(sp.timeout_cb, timeout_cb)
        self.assertEqual(sp.retries, 10)

        timeout_cb.cancel()

    def test_repr(self):
        rudp_packet = json.dumps(
            packet.Packet(
                1,
                ('123.45.67.89', 12345),
                ('213.54.76.98', 54321)
            ).to_json()
        )
        timeout = 0.7
        timeout_cb = reactor.callLater(timeout, lambda: None)
        sp = self.spclass(rudp_packet, timeout, timeout_cb, retries=10)

        self.assertEqual(
            repr(sp),
            'ScheduledPacket({0}, {1}, {2}, {3})'.format(
                rudp_packet,
                timeout,
                timeout_cb,
                sp.retries
            )
        )


class TestConnectionAPI(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.public_ip = '123.45.67.89'
        cls.port = 12345
        cls.own_addr = (cls.public_ip, cls.port)
        cls.addr1 = ('132.54.76.98', 54321)
        cls.addr2 = ('231.76.45.89', 15243)

    def setUp(self):
        self.clock = task.Clock()
        connection.REACTOR.callLater = self.clock.callLater

        self.proto_mock = mock.Mock(spec_set=rudp.ConnectionMultiplexer)
        self.handler_mock = mock.Mock(spec_set=connection.Handler)
        self.con = connection.Connection(
            self.proto_mock,
            self.handler_mock,
            self.own_addr,
            self.addr1
        )

    def tearDown(self):
        self.con.shutdown()

    def test_default_init(self):
        self.assertEqual(self.con.handler, self.handler_mock)
        self.assertEqual(self.con.own_addr, self.own_addr)
        self.assertEqual(self.con.dest_addr, self.addr1)
        self.assertEqual(self.con.relay_addr, self.addr1)
        self.assertEqual(self.con.state, connection.State.CONNECTING)

        self.clock.advance(0)

    def test_init_with_relay(self):
        con = connection.Connection(
            self.proto_mock,
            self.handler_mock,
            self.own_addr,
            self.addr1,
            self.addr2
        )

        self.assertEqual(con.handler, self.handler_mock)
        self.assertEqual(con.own_addr, self.own_addr)
        self.assertEqual(con.dest_addr, self.addr1)
        self.assertEqual(con.relay_addr, self.addr2)
        self.assertEqual(self.con.state, connection.State.CONNECTING)

        self.clock.advance(0)
        con.shutdown()

    # == Test CONNECTING state ==

    def test_shutdown_during_connecting(self):
        self.con.shutdown()

        self.assertEqual(self.con.state, connection.State.SHUTDOWN)
        self.handler_mock.handle_shutdown.assert_called_once_with()

    def test_send_syn_during_connecting(self):
        self.clock.advance(0)
        connection.REACTOR.runUntilCurrent()

        self._advance_to_fin()

        m_calls = self.proto_mock.send_datagram.call_args_list
        self.assertEqual(len(m_calls), constants.MAX_RETRANSMISSIONS + 1)

        first_syn_call = m_calls[0]
        syn_packet = json.loads(first_syn_call[0][0])
        address = first_syn_call[0][1]

        self.assertEqual(address, self.con.relay_addr)
        self.assertGreater(syn_packet['sequence_number'], 0)
        self.assertLess(syn_packet['sequence_number'], 2**16)

        expected_syn_packet = packet.Packet(
            syn_packet['sequence_number'],
            self.con.dest_addr,
            self.con.own_addr,
            syn=True
        ).to_json()

        for call in m_calls[:-1]:
            self.assertEqual(json.loads(call[0][0]), expected_syn_packet)
            self.assertEqual(call[0][1], address)

        expected_fin_packet = packet.Packet(
            0,
            self.con.dest_addr,
            self.con.own_addr,
            fin=True
        ).to_json()

        self.assertEqual(json.loads(m_calls[-1][0][0]), expected_fin_packet)
        self.assertEqual(m_calls[-1][0][1], address)

    def _advance_to_fin(self):
        for _ in range(constants.MAX_RETRANSMISSIONS):
            # Each advance forces a SYN packet retransmission.
            self.clock.advance(constants.PACKET_TIMEOUT)

        # Force transmission of FIN packet and shutdown.
        self.clock.advance(constants.PACKET_TIMEOUT)

        # Trap any calls after shutdown.
        self.clock.advance(100 * constants.PACKET_TIMEOUT)
        connection.REACTOR.runUntilCurrent()

    def test_send_casual_during_connecting(self):
        self.con.send_message('Yellow Submarine')
        self.clock.advance(100 * constants.PACKET_TIMEOUT)
        connection.REACTOR.runUntilCurrent()
        m_calls = self.proto_mock.send_datagram.call_args_list
        self.assertEqual(len(m_calls), 1)
        self.assertTrue(json.loads(m_calls[0][0][0])['syn'])

    def test_receive_fin_during_connecting(self):
        fin_rudp_packet = packet.Packet(
            0,
            self.con.own_addr,
            self.con.dest_addr,
            fin=True
        )

        self.con.receive_packet(fin_rudp_packet)
        self.clock.advance(0)
        connection.REACTOR.runUntilCurrent()

        self.assertEqual(self.con.state, connection.State.SHUTDOWN)
        self.handler_mock.handle_shutdown.assert_called_once_with()

    def test_receive_ack_during_connecting(self):
        pass

    def test_receive_syn_during_connecting(self):
        remote_seqnum = 42
        remote_syn_packet = packet.Packet(
            remote_seqnum,
            self.con.own_addr,
            self.con.dest_addr,
            syn=True
        )

        self.con.receive_packet(remote_syn_packet)
        self.clock.advance(0)
        connection.REACTOR.runUntilCurrent()
        self.assertEqual(self.con.state, connection.State.CONNECTED)

    def test_receive_synack_during_connecting(self):
        remote_synack_packet = packet.Packet(
            42,
            self.con.own_addr,
            self.con.dest_addr,
            syn=True,
            ack=2**15
        )

        self.con.receive_packet(remote_synack_packet)
        self.assertEqual(self.con.state, connection.State.CONNECTED)

    def test_receive_casual_during_connecting(self):
        remote_casual_packet = packet.Packet(
            42,
            self.con.own_addr,
            self.con.dest_addr,
            ack=2**15
        )

        self.con.receive_packet(remote_casual_packet)
        self.clock.advance(0)
        connection.REACTOR.runUntilCurrent()

        self.assertEqual(self.con.state, connection.State.CONNECTING)
        self.handler_mock.receive_message.assert_not_called()

    # == Test CONNECTED state ==

    def _connecting_to_connected(self):
        remote_synack_packet = packet.Packet(
            42,
            self.con.own_addr,
            self.con.dest_addr,
            ack=0,
            syn=True
        )
        self.con.receive_packet(remote_synack_packet)

        self.clock.advance(0)
        connection.REACTOR.runUntilCurrent()

        self.next_remote_seqnum = 43

        m_calls = self.proto_mock.send_datagram.call_args_list
        sent_syn_packet = json.loads(m_calls[0][0][0])
        seqnum = sent_syn_packet['sequence_number']

        self.handler_mock.reset_mock()
        self.proto_mock.reset_mock()

        self.next_seqnum = seqnum + 1

    def test_send_casual_message_during_connected(self):
        self._connecting_to_connected()
        self.con.send_message("Yellow Submarine")
        self._advance_to_fin()

        # Filter casual packets.
        sent_packets = tuple(
            json.loads(call[0][0])
            for call in self.proto_mock.send_datagram.call_args_list
        )
        sent_casual_packets = tuple(
            sent_packet
            for sent_packet in sent_packets
            if not (sent_packet['syn'] or sent_packet['fin'])
        )

        self.assertEqual(
            len(sent_casual_packets),
            constants.MAX_RETRANSMISSIONS
        )

        expected_casual_packet = packet.Packet(
            self.next_seqnum,
            self.con.dest_addr,
            self.con.own_addr,
            ack=self.next_remote_seqnum,
            payload='Yellow Submarine'
        ).to_json()

        for sent_packet in sent_casual_packets:
            self.assertEqual(sent_packet, expected_casual_packet)

    def test_send_big_casual_message_during_connected(self):
        self._connecting_to_connected()

        big_message = ''.join((
            'a' * constants.UDP_SAFE_SEGMENT_SIZE,
            'b' * constants.UDP_SAFE_SEGMENT_SIZE,
            'c' * constants.UDP_SAFE_SEGMENT_SIZE
        ))
        self.con.send_message(big_message)

        self.clock.advance(constants.PACKET_TIMEOUT)
        connection.REACTOR.runUntilCurrent()
        m_calls = self.proto_mock.send_datagram.call_args_list

        # Filter casual packets.
        sent_packets = tuple(
            json.loads(call[0][0])
            for call in self.proto_mock.send_datagram.call_args_list
        )
        sent_casual_packets = tuple(
            sent_packet
            for sent_packet in sent_packets
            if not (sent_packet['syn'] or sent_packet['fin'])
        )

        self.assertEqual(len(sent_casual_packets), 3)
        expected_casual_packets = tuple(
            packet.Packet(
                self.next_seqnum + i,
                self.con.dest_addr,
                self.con.own_addr,
                ack=self.next_remote_seqnum,
                payload=payload * constants.UDP_SAFE_SEGMENT_SIZE,
                more_fragments=2 - i
            ).to_json()
            for i, payload in zip(range(3), 'abc')
        )

        self.assertEqual(sent_casual_packets, expected_casual_packets)

    def test_receive_casual_packet_during_connected(self):
        self._connecting_to_connected()

        remote_casual_packet = packet.Packet(
            self.next_remote_seqnum,
            self.con.own_addr,
            self.con.dest_addr,
            payload='Yellow Submarine',
            ack=self.next_seqnum
        )
        self.con.receive_packet(remote_casual_packet)

        self.clock.advance(0)
        connection.REACTOR.runUntilCurrent()

        self.handler_mock.receive_message.assert_called_once_with(
            'Yellow Submarine'
        )

    def test_receive_casual_packets_during_connected(self):
        self._connecting_to_connected()

        payloads = ('a', 'b', 'c')
        remote_casual_packets = tuple(
            packet.Packet(
                self.next_remote_seqnum + i,
                self.con.own_addr,
                self.con.dest_addr,
                payload=payload,
                ack=self.next_seqnum
            )
            for i, payload in enumerate(payloads)
        )

        for p in reversed(remote_casual_packets):
            self.con.receive_packet(p)

        self.clock.advance(0)
        connection.REACTOR.runUntilCurrent()

        r_calls = self.handler_mock.receive_message.call_args_list
        messages = tuple(call[0][0] for call in r_calls)
        self.assertEqual(payloads, messages)

    def test_receive_fragmented_packet_during_connected(self):
        self._connecting_to_connected()

        messages = (
            'a' * constants.UDP_SAFE_SEGMENT_SIZE,
            'b' * constants.UDP_SAFE_SEGMENT_SIZE,
            'c' * constants.UDP_SAFE_SEGMENT_SIZE,
        )
        remote_casual_packets = tuple(
            packet.Packet(
                self.next_remote_seqnum + i,
                self.con.own_addr,
                self.con.dest_addr,
                payload=payload,
                ack=self.next_seqnum,
                more_fragments=len(messages) - i - 1
            )
            for i, payload in enumerate(messages)
        )

        for p in reversed(remote_casual_packets):
            self.con.receive_packet(p)

        self.clock.advance(0)
        connection.REACTOR.runUntilCurrent()

        self.handler_mock.receive_message.assert_called_once_with(
            ''.join(messages)
        )

    # == Test SHUTDOWN state ==

    def test_send_casual_during_shutdown(self):
        self._connecting_to_connected()
        self.con.shutdown()

        self.con.send_message("Yellow Submarine")

        self.clock.advance(100 * constants.PACKET_TIMEOUT)
        connection.REACTOR.runUntilCurrent()

        self.assertEqual(self.con.state, connection.State.SHUTDOWN)
        self.proto_mock.send_datagram.assert_not_called()

    def test_receive_syn_during_shutdown(self):
        pass

    def test_receive_synack_during_shutdown(self):
        pass

    def test_receive_ack_during_shutdown(self):
        pass

    def test_receive_fin_during_shutdown(self):
        pass

    def test_receive_casual_during_shutdown(self):
        self._connecting_to_connected()
        self.con.shutdown()

        self.handler_mock.reset_mock()

        casual_rudp_packet = packet.Packet(
            self.next_seqnum,
            self.con.dest_addr,
            self.con.own_addr,
            ack=0,
            payload='Yellow Submarine'
        )
        self.con.receive_packet(casual_rudp_packet)

        self.clock.advance(100 * constants.PACKET_TIMEOUT)
        connection.REACTOR.runUntilCurrent()

        self.assertEqual(self.con.state, connection.State.SHUTDOWN)
        self.handler_mock.receive_message.assert_not_called()
