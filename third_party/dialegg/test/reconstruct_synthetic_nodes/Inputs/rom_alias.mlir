module {
  hw.module @rom_alias(in %a : i8, out d : i1) {
    %c0 = hw.constant 0 : i8
    %t = hw.constant true
    %0 = comb.icmp eq %a, %c0 : i8
    %1 = comb.xor %0, %t : i1
    %2 = comb.and %1, %0 : i1
    hw.output %2 : i1
  }
}
