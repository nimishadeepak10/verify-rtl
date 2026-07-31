// Synchronous FIFO: 4 entries, 8-bit wide, single clock domain.
// The canonical "next rung up" complexity test after single-FSM designs —
// FIFOs are the standard formal-verification teaching example (riscv-formal,
// ZipCPU, and the SymbiYosys docs all use one) precisely because there's an
// established, known-correct property set to validate a suggestion engine
// against, rather than just eyeballing plausibility.
module sync_fifo (
    input  wire       clk,
    input  wire       rst_n,
    input  wire       wr_en,
    input  wire [7:0] wr_data,
    input  wire       rd_en,
    output wire [7:0] rd_data,
    output wire       full,
    output wire       empty,
    output wire [2:0] count
);
    reg [7:0] mem [0:3];
    reg [2:0] wr_ptr;
    reg [2:0] rd_ptr;

    wire wr_valid = wr_en && !full;
    wire rd_valid = rd_en && !empty;

    always @(posedge clk or negedge rst_n) begin
        if (!rst_n)
            wr_ptr <= 3'd0;
        else if (wr_valid) begin
            mem[wr_ptr[1:0]] <= wr_data;
            wr_ptr <= wr_ptr + 3'd1;
        end
    end

    always @(posedge clk or negedge rst_n) begin
        if (!rst_n)
            rd_ptr <= 3'd0;
        else if (rd_valid)
            rd_ptr <= rd_ptr + 3'd1;
    end

    assign rd_data = mem[rd_ptr[1:0]];
    assign full  = (wr_ptr[2] != rd_ptr[2]) && (wr_ptr[1:0] == rd_ptr[1:0]);
    assign empty = (wr_ptr == rd_ptr);
    assign count = wr_ptr - rd_ptr;
endmodule
