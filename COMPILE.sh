cairo-compile --cairo_path=./src src/starkware/cairo/cairo_verifier/layouts/starknet_with_keccak/cairo_verifier.cairo --output cairo_verifier.json --no_debug_info

cairo-run \
    --program=cairo_verifier.json \
    --layout=starknet_with_keccak \
    --program_input=cairo_verifier_input.json \
    --trace_file=cairo_verifier_trace.json \
    --memory_file=cairo_verifier_memory.json \
    --print_output