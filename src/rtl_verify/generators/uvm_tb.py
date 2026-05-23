"""UVM testbench skeleton (requires UVM-capable simulator: Questa, VCS, Xcelium)."""

from __future__ import annotations

from ..analyzer import PortDirection, RtlModule


def generate(mod: RtlModule) -> str:
    inputs = mod.inputs
    outputs = mod.outputs
    if not inputs:
        raise ValueError("UVM skeleton requires at least one input port")

    in_decl = "\n".join(
        f"  rand bit {p.range_str()} {p.name};" if not p.is_scalar else f"  rand bit {p.name};"
        for p in inputs
    )
    drive = "\n".join(
        f"    vif.{p.name} <= tr.{p.name};" for p in inputs
    )
    monitor_fmt = " ".join(f"{p.name}=%0d" for p in outputs) if outputs else "cycle"
    monitor_args = ", ".join(f"vif.{p.name}" for p in outputs) if outputs else ""

    return f"""// Auto-generated UVM testbench for {mod.name}
// NOTE: Compile with a UVM-enabled simulator (e.g. Questa: -uvmhome $UVM_HOME)
`include "uvm_macros.svh"
import uvm_pkg::*;

interface {mod.name}_if;
{_iface_signals(mod)}
endinterface

class {mod.name}_seq_item extends uvm_sequence_item;
{in_decl}
  `uvm_object_utils({mod.name}_seq_item)
  function new(string name = "{mod.name}_seq_item");
    super.new(name);
  endfunction
endclass

class {mod.name}_seq extends uvm_sequence #({mod.name}_seq_item);
  `uvm_object_utils({mod.name}_seq)
  function new(string name = "{mod.name}_seq");
    super.new(name);
  endfunction
  task body();
    {mod.name}_seq_item tr;
    for (int i = 0; i < 16; i++) begin
      tr = {mod.name}_seq_item::type_id::create("tr");
      start_item(tr);
      if (!tr.randomize()) `uvm_fatal("SEQ", "randomize failed")
      finish_item(tr);
      #10;
    end
  endtask
endclass

class {mod.name}_driver extends uvm_driver #({mod.name}_seq_item);
  virtual {mod.name}_if vif;
  `uvm_component_utils({mod.name}_driver)
  function new(string name, uvm_component parent);
    super.new(name, parent);
  endfunction
  task run_phase(uvm_phase phase);
    forever begin
      seq_item_port.get_next_item(req);
{drive}
      seq_item_port.item_done();
      #5;
    end
  endtask
endclass

class {mod.name}_monitor extends uvm_monitor;
  virtual {mod.name}_if vif;
  `uvm_component_utils({mod.name}_monitor)
  function new(string name, uvm_component parent);
    super.new(name, parent);
  endfunction
  task run_phase(uvm_phase phase);
    forever begin
      #5;
      `uvm_info("MON", $sformatf("{monitor_fmt}", {monitor_args}), UVM_MEDIUM)
    end
  endtask
endclass

class {mod.name}_env extends uvm_env;
  {mod.name}_driver drv;
  {mod.name}_monitor mon;
  `uvm_component_utils({mod.name}_env)
  function new(string name, uvm_component parent);
    super.new(name, parent);
  endfunction
  function void build_phase(uvm_phase phase);
    drv = {mod.name}_driver::type_id::create("drv", this);
    mon = {mod.name}_monitor::type_id::create("mon", this);
  endfunction
  function void connect_phase(uvm_phase phase);
    drv.seq_item_port.connect(sequencer.seq_item_export);
  endfunction
endclass

module tb_{mod.name};
  {mod.name}_if vif();
  {mod.name} dut (
{_dut_conn(mod)}
  );
  initial begin
    uvm_config_db#(virtual {mod.name}_if)::set(null, "*", "vif", vif);
    run_test("{mod.name}_test");
  end
endmodule

class {mod.name}_test extends uvm_test;
  {mod.name}_env env;
  `uvm_component_utils({mod.name}_test)
  function new(string name, uvm_component parent);
    super.new(name, parent);
  endfunction
  function void build_phase(uvm_phase phase);
    env = {mod.name}_env::type_id::create("env", this);
  endfunction
  task run_phase(uvm_phase phase);
    {mod.name}_seq seq;
    phase.raise_objection(this);
    seq = {mod.name}_seq::type_id::create("seq");
    seq.start(env.drv.sequencer);
    phase.drop_objection(this);
  endtask
endclass
"""


def _iface_signals(mod: RtlModule) -> str:
    lines = []
    for p in mod.ports:
        r = p.range_str()
        if p.direction == PortDirection.INPUT:
            lines.append(f"  logic {r} {p.name};")
        else:
            lines.append(f"  logic {r} {p.name};")
    return "\n".join(lines)


def _dut_conn(mod: RtlModule) -> str:
    return "\n".join(f"    .{p.name}(vif.{p.name})" for p in mod.ports)
