#include <stdio.h>
#include "fec.h"

int main() {
    unsigned char data[223] = {0};
    unsigned char parity[32];

    void* rs = init_rs_char(8, 0x11d, 1, 1, 32, 0);
    if (!rs) {
        printf("RS init failed\n");
        return 1;
    }

    encode_rs_char(rs, data, parity);
    printf("Encode success\n");

    int err = decode_rs_char(rs, data, NULL, 0);
    printf("Decode result: %d\n", err);

    return 0;
}