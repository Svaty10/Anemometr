/**
 * @file proto.h
 * @brief Pure (hardware independent) packet functions of the anemometer protocol – shared by the
 *        firmware and the host interoperability test.
 */
#ifndef PROTO_H_
#define PROTO_H_

#include <stdint.h>
#include <stdbool.h>

/** Kenc/Kmac from the X25519 shared secret. Returns false for an all-zero secret. */
bool proto_derive_keys(const uint8_t shared[32], uint8_t k_enc[16], uint8_t k_mac[16]);

/* Own packet types: the tank hub (0x03..0x06) never mixes them up. */
#define PROTO_TYPE_DATA     0x13U
#define PROTO_TYPE_UNPAIR   0x16U

#define PROTO_TELEMETRY_LEN 12U
#define PROTO_DATA_LEN      23U     /* 7 header + 12 ciphertext + 4 MAC */

/** Build a 23-byte DATA packet from a 12-byte telemetry record. */
void proto_build_data(uint8_t out[PROTO_DATA_LEN], const uint8_t k_enc[16], const uint8_t k_mac[16],
                      uint16_t node_id, uint32_t ctr, const uint8_t telemetry[PROTO_TELEMETRY_LEN]);

/**
 * Verify an UNPAIR packet: [0x16][node_id 2][CMAC(Kmac, 0x16|node_id|node_pub) 16].
 * The tag is bound to this boot's public key, so a recorded UNPAIR cannot be replayed.
 */
bool proto_check_unpair(const uint8_t *pkt, uint8_t len, const uint8_t k_mac[16],
                        uint16_t node_id, const uint8_t node_pub[32]);

#endif /* PROTO_H_ */
