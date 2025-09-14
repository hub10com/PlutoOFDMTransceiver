#include "fec.h"

void encode_rs_char(void *rs, unsigned char *data, unsigned char *parity) {
    for (int i = 0; i < 32; ++i)
        parity[i] = 0; // Dummy parity
}