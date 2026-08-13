module priority_weighted_arbiter #(
    parameter int NUM_REQUESTORS = 8,
    parameter int WEIGHT_WIDTH   = 4,
    parameter int GRANT_WIDTH    = $clog2(NUM_REQUESTORS)
)(
    input  logic [NUM_REQUESTORS-1:0]                  req,
    input  logic [NUM_REQUESTORS-1:0][WEIGHT_WIDTH-1:0] weight,
    input  logic [GRANT_WIDTH-1:0]                     last_grant,
    output logic [NUM_REQUESTORS-1:0]                  grant,
    output logic [GRANT_WIDTH-1:0]                     grant_idx,
    output logic                                        grant_valid,
    output logic [WEIGHT_WIDTH-1:0]                    grant_weight
);

    logic [NUM_REQUESTORS-1:0] rr_mask;
    logic [NUM_REQUESTORS-1:0] masked_req;
    logic [NUM_REQUESTORS-1:0] active_req;

    always_comb begin
        for (int i = 0; i < NUM_REQUESTORS; i++)
            rr_mask[i] = (i > last_grant);
    end

    assign masked_req = req & rr_mask;
    assign active_req = (masked_req != '0) ? masked_req : req;

    logic [WEIGHT_WIDTH-1:0] max_weight;

    always_comb begin
        max_weight = '0;
        for (int i = 0; i < NUM_REQUESTORS; i++)
            if (active_req[i] && (weight[i] > max_weight))
                max_weight = weight[i];
    end

    logic [NUM_REQUESTORS-1:0] weight_filtered_req;

    always_comb begin
        for (int i = 0; i < NUM_REQUESTORS; i++)
            weight_filtered_req[i] = active_req[i] && (weight[i] == max_weight);
    end

    logic [NUM_REQUESTORS-1:0] priority_grant;
    logic                       found;

    always_comb begin
        priority_grant = '0;
        found          = 1'b0;
        for (int i = 0; i < NUM_REQUESTORS; i++)
            if (weight_filtered_req[i] && !found) begin
                priority_grant[i] = 1'b1;
                found             = 1'b1;
            end
    end

    always_comb begin
        grant_idx = '0;
        for (int i = 0; i < NUM_REQUESTORS; i++)
            if (priority_grant[i])
                grant_idx = GRANT_WIDTH'(i);
    end

    assign grant        = priority_grant;
    assign grant_valid  = (req != '0);
    assign grant_weight = grant_valid ? max_weight : '0;

endmodule
