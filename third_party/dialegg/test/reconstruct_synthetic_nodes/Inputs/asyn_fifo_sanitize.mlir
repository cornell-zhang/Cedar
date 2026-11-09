module {
  hw.module @asyn_fifo_sanitize(in %clk : i1, in %d : i8, out q : i8) {
    %c = seq.to_clock %clk
    %r = seq.firreg %d clock %c {name = "dual_port_RAM/rdata"} : i8
    hw.output %r : i8
  }
}
