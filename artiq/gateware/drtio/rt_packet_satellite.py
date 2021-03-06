"""Real-time packet layer for satellites"""

from migen import *
from migen.genlib.fsm import *

from artiq.gateware.rtio import cri
from artiq.gateware.drtio.rt_serializer import *


class RTPacketSatellite(Module):
    def __init__(self, link_layer):
        self.unknown_packet_type = Signal()
        self.packet_truncated = Signal()

        self.tsc_load = Signal()
        self.tsc_load_value = Signal(64)

        self.reset = Signal(reset=1)
        self.reset_phy = Signal(reset=1)

        self.cri = cri.Interface()

        # # #

        # RX/TX datapath
        assert len(link_layer.tx_rt_data) == len(link_layer.rx_rt_data)
        assert len(link_layer.tx_rt_data) % 8 == 0
        ws = len(link_layer.tx_rt_data)
        rx_plm = get_m2s_layouts(ws)
        rx_dp = ReceiveDatapath(
            link_layer.rx_rt_frame, link_layer.rx_rt_data, rx_plm)
        self.submodules += rx_dp
        tx_plm = get_s2m_layouts(ws)
        tx_dp = TransmitDatapath(
            link_layer.tx_rt_frame, link_layer.tx_rt_data, tx_plm)
        self.submodules += tx_dp

        # RX write data buffer
        write_data_buffer_load = Signal()
        write_data_buffer_cnt = Signal(max=512//ws+1)
        write_data_buffer = Signal(512)
        self.sync += \
            If(write_data_buffer_load,
                Case(write_data_buffer_cnt,
                     {i: write_data_buffer[i*ws:(i+1)*ws].eq(rx_dp.data_r)
                      for i in range(512//ws)}),
                write_data_buffer_cnt.eq(write_data_buffer_cnt + 1)
            ).Else(
                write_data_buffer_cnt.eq(0)
            )

        # RX->TX
        echo_req = Signal()
        buffer_space_set = Signal()
        buffer_space_req = Signal()
        buffer_space_ack = Signal()
        self.sync += [
            If(buffer_space_ack, buffer_space_req.eq(0)),
            If(buffer_space_set, buffer_space_req.eq(1)),
        ]

        buffer_space_update = Signal()
        buffer_space = Signal(16)
        self.sync += If(buffer_space_update, buffer_space.eq(self.cri.o_buffer_space))

        load_read_request = Signal()
        clear_read_request = Signal()
        read_request_pending = Signal()
        self.sync += [
            If(clear_read_request | self.reset,
                read_request_pending.eq(0)
            ),
            If(load_read_request,
                read_request_pending.eq(1),
            )
        ]

        # RX FSM
        read = Signal()
        self.comb += [
            self.tsc_load_value.eq(
                rx_dp.packet_as["set_time"].timestamp),
            If(load_read_request | read_request_pending,
                self.cri.chan_sel.eq(
                    rx_dp.packet_as["read_request"].channel),
                self.cri.timestamp.eq(
                    rx_dp.packet_as["read_request"].timeout)
            ).Else(
                self.cri.chan_sel.eq(
                    rx_dp.packet_as["write"].channel),
                self.cri.timestamp.eq(
                    rx_dp.packet_as["write"].timestamp)
            ),
            self.cri.o_address.eq(
                rx_dp.packet_as["write"].address),
            self.cri.o_data.eq(
                Cat(rx_dp.packet_as["write"].short_data, write_data_buffer)),
        ]

        reset = Signal()
        reset_phy = Signal()
        self.sync += [
            self.reset.eq(reset),
            self.reset_phy.eq(reset_phy)
        ]

        rx_fsm = FSM(reset_state="INPUT")
        self.submodules += rx_fsm

        ongoing_packet_next = Signal()
        ongoing_packet = Signal()
        self.sync += ongoing_packet.eq(ongoing_packet_next)

        rx_fsm.act("INPUT",
            If(rx_dp.frame_r,
                rx_dp.packet_buffer_load.eq(1),
                If(rx_dp.packet_last,
                    Case(rx_dp.packet_type, {
                        # echo must have fixed latency, so there is no memory
                        # mechanism
                        rx_plm.types["echo_request"]: echo_req.eq(1),
                        rx_plm.types["set_time"]: NextState("SET_TIME"),
                        rx_plm.types["reset"]: NextState("RESET"),
                        rx_plm.types["write"]: NextState("WRITE"),
                        rx_plm.types["buffer_space_request"]: NextState("BUFFER_SPACE"),
                        rx_plm.types["read_request"]: NextState("READ_REQUEST"),
                        "default": self.unknown_packet_type.eq(1)
                    })
                ).Else(
                    ongoing_packet_next.eq(1)
                ),
                If(~rx_dp.frame_r & ongoing_packet,
                    self.packet_truncated.eq(1)
                )
            )
        )
        rx_fsm.act("SET_TIME",
            self.tsc_load.eq(1),
            NextState("INPUT")
        )
        rx_fsm.act("RESET",
            If(rx_dp.packet_as["reset"].phy,
                reset_phy.eq(1)
            ).Else(
                reset.eq(1)
            ),
            NextState("INPUT")
        )

        rx_fsm.act("WRITE",
            If(write_data_buffer_cnt == rx_dp.packet_as["write"].extra_data_cnt,
                self.cri.cmd.eq(cri.commands["write"]),
                NextState("INPUT")
            ).Else(
                write_data_buffer_load.eq(1),
                If(~rx_dp.frame_r,
                    self.packet_truncated.eq(1),
                    NextState("INPUT")
                )
            )
        )
        rx_fsm.act("BUFFER_SPACE",
            buffer_space_set.eq(1),
            buffer_space_update.eq(1),
            NextState("INPUT")
        )

        rx_fsm.act("READ_REQUEST",
            load_read_request.eq(1),
            self.cri.cmd.eq(cri.commands["read"]),
            NextState("INPUT")
        )

        # TX FSM
        tx_fsm = FSM(reset_state="IDLE")
        self.submodules += tx_fsm

        tx_fsm.act("IDLE",
            If(echo_req, NextState("ECHO")),
            If(buffer_space_req, NextState("BUFFER_SPACE")),
            If(read_request_pending,
                If(~self.cri.i_status[2], NextState("READ")),
                If(self.cri.i_status[0], NextState("READ_TIMEOUT")),
                If(self.cri.i_status[1], NextState("READ_OVERFLOW"))
            )
        )

        tx_fsm.act("ECHO",
            tx_dp.send("echo_reply"),
            If(tx_dp.packet_last, NextState("IDLE"))
        )

        tx_fsm.act("BUFFER_SPACE",
            buffer_space_ack.eq(1),
            tx_dp.send("buffer_space_reply", space=buffer_space),
            If(tx_dp.packet_last, NextState("IDLE"))
        )

        tx_fsm.act("READ_TIMEOUT",
            tx_dp.send("read_reply_noevent", overflow=0),
            clear_read_request.eq(1),
            If(tx_dp.packet_last, NextState("IDLE"))
        )
        tx_fsm.act("READ_OVERFLOW",
            tx_dp.send("read_reply_noevent", overflow=1),
            clear_read_request.eq(1),
            If(tx_dp.packet_last,
                NextState("IDLE")
            )
        )
        tx_fsm.act("READ",
            tx_dp.send("read_reply",
                       timestamp=self.cri.i_timestamp,
                       data=self.cri.i_data),
            clear_read_request.eq(1),
            If(tx_dp.packet_last,
                NextState("IDLE")
            )
        )
