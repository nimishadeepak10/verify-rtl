// Single-cycle RV32I core with a real RVFI (RISC-V Formal Interface) output,
// per the official spec (github.com/YosysHQ/riscv-formal, docs/source/rvfi.rst
// -- fetched and read before writing this, not assumed). NRET=1 (single-
// issue), XLEN=32, ILEN=32, no compressed instructions, no CSRs (any system
// instruction other than a plain ECALL/EBREAK-style trap is treated as
// illegal). Full RV32I base integer instruction set.
//
// Harvard-style: imem/dmem are external, combinational-read interfaces (the
// core drives an address, the environment returns data the same cycle --
// consistent with riscv-formal's own reference-core memory model), so the
// only clocked state inside this module is the PC, the register file, and
// the RVFI retirement-order counter.
//
// RVFI outputs are driven COMBINATIONALLY from the same signals that drive
// the state update each cycle: pc_rdata/rs1_rdata/rs2_rdata are the current
// (pre-instruction) state; rd_wdata/pc_wdata are the values about to be
// latched at this same clock edge. This is the central methodological point
// of this design: unlike every earlier multi-cycle design in this project,
// RVFI exposes everything needed to check ONE instruction's correctness on
// a SINGLE clock edge, sidestepping the live-port-vs-captured-value and
// multi-cycle-claim limitations found testing the divider and cache.
//
// On trap (illegal instruction, misaligned jump/branch target, or any
// system instruction), the core halts in place (pc_next = pc) rather than
// modeling a trap handler -- there's no privileged mode or CSR file here.
module rv32i_core (
    input  wire        clk,
    input  wire        rst_n,

    output wire [31:0] imem_addr,
    input  wire [31:0] imem_rdata,

    output wire         dmem_wen,
    output wire [31:0]  dmem_addr,
    output wire [3:0]   dmem_wmask,
    output wire [31:0]  dmem_wdata,
    input  wire [31:0]  dmem_rdata,

    output wire          rvfi_valid,
    output wire [63:0]  rvfi_order,
    output wire [31:0]  rvfi_insn,
    output wire          rvfi_trap,
    output wire          rvfi_halt,
    output wire          rvfi_intr,
    output wire [1:0]   rvfi_mode,
    output wire [1:0]   rvfi_ixl,
    output wire [4:0]   rvfi_rs1_addr,
    output wire [4:0]   rvfi_rs2_addr,
    output wire [31:0]  rvfi_rs1_rdata,
    output wire [31:0]  rvfi_rs2_rdata,
    output wire [4:0]   rvfi_rd_addr,
    output wire [31:0]  rvfi_rd_wdata,
    output wire [31:0]  rvfi_pc_rdata,
    output wire [31:0]  rvfi_pc_wdata,
    output wire [31:0]  rvfi_mem_addr,
    output wire [3:0]   rvfi_mem_rmask,
    output wire [3:0]   rvfi_mem_wmask,
    output wire [31:0]  rvfi_mem_rdata,
    output wire [31:0]  rvfi_mem_wdata
);
    reg [31:0] pc;
    reg [31:0] regfile [1:31]; // x0 hardwired to 0, never stored
    reg [63:0] order_ctr;

    wire [31:0] insn = imem_rdata;
    assign imem_addr = pc;

    // decode
    wire [6:0] opcode     = insn[6:0];
    wire [4:0] rd_addr_w  = insn[11:7];
    wire [2:0] funct3     = insn[14:12];
    wire [4:0] rs1_addr_w = insn[19:15];
    wire [4:0] rs2_addr_w = insn[24:20];
    wire [6:0] funct7     = insn[31:25];

    wire is_rtype  = (opcode == 7'b0110011);
    wire is_itype  = (opcode == 7'b0010011);
    wire is_load   = (opcode == 7'b0000011);
    wire is_store  = (opcode == 7'b0100011);
    wire is_branch = (opcode == 7'b1100011);
    wire is_jal    = (opcode == 7'b1101111);
    wire is_jalr   = (opcode == 7'b1100111 && funct3 == 3'b000);
    wire is_lui    = (opcode == 7'b0110111);
    wire is_auipc  = (opcode == 7'b0010111);
    wire is_fence  = (opcode == 7'b0001111);
    wire is_system = (opcode == 7'b1110011);

    wire insn_known = is_rtype || is_itype || is_load || is_store || is_branch ||
                       is_jal || is_jalr || is_lui || is_auipc || is_fence || is_system;

    // immediates
    wire [31:0] imm_i = {{20{insn[31]}}, insn[31:20]};
    wire [31:0] imm_s = {{20{insn[31]}}, insn[31:25], insn[11:7]};
    wire [31:0] imm_b = {{19{insn[31]}}, insn[31], insn[7], insn[30:25], insn[11:8], 1'b0};
    wire [31:0] imm_u = {insn[31:12], 12'd0};
    wire [31:0] imm_j = {{11{insn[31]}}, insn[31], insn[19:12], insn[20], insn[30:21], 1'b0};

    // register read (combinational, x0 hardwired zero)
    wire [31:0] rs1_rdata = (rs1_addr_w == 5'd0) ? 32'd0 : regfile[rs1_addr_w];
    wire [31:0] rs2_rdata = (rs2_addr_w == 5'd0) ? 32'd0 : regfile[rs2_addr_w];

    wire [31:0] alu_op2 = (is_rtype || is_branch) ? rs2_rdata : imm_i;
    wire signed [31:0] rs1_s = rs1_rdata;
    wire signed [31:0] op2_s = alu_op2;
    wire lt_signed   = rs1_s < op2_s;
    wire lt_unsigned = rs1_rdata < alu_op2;
    wire [4:0] shamt = is_rtype ? rs2_rdata[4:0] : imm_i[4:0];

    // rs1_s (declared above, next to lt_signed) is reused below for the
    // arithmetic-shift ALU cases. Deliberately NOT inlined as
    // $signed(rs1_rdata) at the point of use: confirmed empirically
    // (isolated Icarus test) AND against IEEE 1800-2023 SS11.8.1 ("If any
    // operand is unsigned, the result is unsigned, regardless of the
    // operator") that a ternary whose two branches differ in signedness --
    // e.g. `cond ? ($signed(x) >>> n) : (x >> n)` -- silently degrades the
    // WHOLE expression to unsigned, turning the arithmetic shift into a
    // logical one even though the cast is textually right there. A plain
    // if/else has no such rule and was confirmed correct in the same test,
    // so SRA/SRAI below use if/else, not a ternary, despite every other ALU
    // case using one. See docs/systemverilog_ieee1800_rules.md SS1 for the
    // full writeup (this bug, and the near-identical one it caused again in
    // scripts/test_rv32i_rvfi_checks.py's own hand-written properties).

    reg [31:0] alu_result;
    always @* begin
        if (is_rtype) begin
            case (funct3)
                3'b000:  alu_result = funct7[5] ? (rs1_rdata - rs2_rdata) : (rs1_rdata + rs2_rdata);
                3'b001:  alu_result = rs1_rdata << shamt;
                3'b010:  alu_result = {31'd0, lt_signed};
                3'b011:  alu_result = {31'd0, lt_unsigned};
                3'b100:  alu_result = rs1_rdata ^ rs2_rdata;
                3'b101:  if (funct7[5]) alu_result = rs1_s >>> shamt; else alu_result = rs1_rdata >> shamt;
                3'b110:  alu_result = rs1_rdata | rs2_rdata;
                3'b111:  alu_result = rs1_rdata & rs2_rdata;
                default: alu_result = 32'd0;
            endcase
        end else if (is_itype) begin
            case (funct3)
                3'b000:  alu_result = rs1_rdata + imm_i;
                3'b010:  alu_result = {31'd0, lt_signed};
                3'b011:  alu_result = {31'd0, lt_unsigned};
                3'b100:  alu_result = rs1_rdata ^ imm_i;
                3'b110:  alu_result = rs1_rdata | imm_i;
                3'b111:  alu_result = rs1_rdata & imm_i;
                3'b001:  alu_result = rs1_rdata << shamt;
                3'b101:  if (insn[30]) alu_result = rs1_s >>> shamt; else alu_result = rs1_rdata >> shamt;
                default: alu_result = 32'd0;
            endcase
        end else begin
            alu_result = 32'd0;
        end
    end

    // branch condition
    reg branch_taken;
    always @* begin
        case (funct3)
            3'b000:  branch_taken = (rs1_rdata == rs2_rdata); // BEQ
            3'b001:  branch_taken = (rs1_rdata != rs2_rdata); // BNE
            3'b100:  branch_taken = lt_signed;                // BLT
            3'b101:  branch_taken = !lt_signed;               // BGE
            3'b110:  branch_taken = lt_unsigned;               // BLTU
            3'b111:  branch_taken = !lt_unsigned;              // BGEU
            default: branch_taken = 1'b0;
        endcase
    end

    // memory address / byte mask
    wire [31:0] mem_addr_w = rs1_rdata + (is_load ? imm_i : imm_s);
    wire [1:0]  byte_off   = mem_addr_w[1:0];

    reg [3:0] mem_mask;
    always @* begin
        case (funct3[1:0])
            2'b00:   mem_mask = 4'b0001 << byte_off; // byte
            2'b01:   mem_mask = 4'b0011 << byte_off; // halfword
            default: mem_mask = 4'b1111;             // word
        endcase
    end

    assign dmem_addr  = mem_addr_w;
    assign dmem_wen   = is_store && insn_known;
    assign dmem_wmask = is_store ? mem_mask : 4'b0000;
    assign dmem_wdata = rs2_rdata << (byte_off * 8);

    wire [31:0] shifted_rdata = dmem_rdata >> (byte_off * 8);
    reg [31:0] load_result;
    always @* begin
        case (funct3)
            3'b000:  load_result = {{24{shifted_rdata[7]}},  shifted_rdata[7:0]};  // LB
            3'b001:  load_result = {{16{shifted_rdata[15]}}, shifted_rdata[15:0]}; // LH
            3'b010:  load_result = shifted_rdata;                                   // LW
            3'b100:  load_result = {24'd0, shifted_rdata[7:0]};                     // LBU
            3'b101:  load_result = {16'd0, shifted_rdata[15:0]};                    // LHU
            default: load_result = 32'd0;
        endcase
    end

    wire [31:0] jal_target    = pc + imm_j;
    wire [31:0] jalr_target   = (rs1_rdata + imm_i) & ~32'd1;
    wire [31:0] branch_target = pc + imm_b;

    wire misaligned_jal    = is_jal    && jal_target[1:0]    != 2'b00;
    wire misaligned_jalr   = is_jalr   && jalr_target[1:0]   != 2'b00;
    wire misaligned_branch = is_branch && branch_taken && branch_target[1:0] != 2'b00;

    wire trap_w = !insn_known || is_system || misaligned_jal || misaligned_jalr || misaligned_branch;

    reg [31:0] rd_wdata_w;
    always @* begin
        if (is_jal || is_jalr) rd_wdata_w = pc + 32'd4;
        else if (is_lui)       rd_wdata_w = imm_u;
        else if (is_auipc)     rd_wdata_w = pc + imm_u;
        else if (is_load)      rd_wdata_w = load_result;
        else                     rd_wdata_w = alu_result; // R-type / I-type ALU
    end

    wire rd_writes = !trap_w &&
        (is_rtype || is_itype || is_load || is_lui || is_auipc || is_jal || is_jalr) &&
        (rd_addr_w != 5'd0);

    reg [31:0] pc_next;
    always @* begin
        if (trap_w)                          pc_next = pc; // halt in place, no trap handler modeled
        else if (is_jal)                     pc_next = jal_target;
        else if (is_jalr)                    pc_next = jalr_target;
        else if (is_branch && branch_taken)  pc_next = branch_target;
        else                                  pc_next = pc + 32'd4;
    end

    integer i;
    always @(posedge clk or negedge rst_n) begin
        if (!rst_n) begin
            pc <= 32'd0;
            order_ctr <= 64'd0;
            for (i = 1; i <= 31; i = i + 1) regfile[i] <= 32'd0;
        end else begin
            pc <= pc_next;
            order_ctr <= order_ctr + 64'd1;
            if (rd_writes) regfile[rd_addr_w] <= rd_wdata_w;
        end
    end

    // RVFI: combinational mirror of this cycle's retiring instruction
    assign rvfi_valid = rst_n;
    assign rvfi_order = order_ctr;
    assign rvfi_insn  = insn;
    assign rvfi_trap  = trap_w;
    assign rvfi_halt  = 1'b0;
    assign rvfi_intr  = 1'b0;
    assign rvfi_mode  = 2'd3; // M-mode always; no privilege levels modeled
    assign rvfi_ixl   = 2'd1; // 32-bit

    assign rvfi_rs1_addr = (is_rtype || is_itype || is_load || is_store || is_branch || is_jalr) ? rs1_addr_w : 5'd0;
    assign rvfi_rs2_addr = (is_rtype || is_store || is_branch) ? rs2_addr_w : 5'd0;
    assign rvfi_rs1_rdata = rs1_rdata;
    assign rvfi_rs2_rdata = rs2_rdata;
    assign rvfi_rd_addr  = rd_writes ? rd_addr_w  : 5'd0;
    assign rvfi_rd_wdata = rd_writes ? rd_wdata_w : 32'd0;
    assign rvfi_pc_rdata = pc;
    assign rvfi_pc_wdata = pc_next;

    assign rvfi_mem_addr  = (is_load || is_store) ? mem_addr_w : 32'd0;
    assign rvfi_mem_rmask = (is_load  && !trap_w) ? mem_mask : 4'd0;
    assign rvfi_mem_wmask = (is_store && !trap_w) ? mem_mask : 4'd0;
    assign rvfi_mem_rdata = is_load  ? dmem_rdata : 32'd0;
    assign rvfi_mem_wdata = is_store ? dmem_wdata : 32'd0;
endmodule
