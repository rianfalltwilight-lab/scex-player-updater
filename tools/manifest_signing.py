"""Sign a manifest with a .NET RSA XML private key (pure Python; test helper and release tool core)."""
import base64
import hashlib
import json
import re
import sys

DIGEST_INFO = bytes.fromhex("3031300d060960864801650304020105000420")


def load_xml_key(text):
    fields = {name: int.from_bytes(base64.b64decode(value), "big")
              for name, value in re.findall(r"<(\w+)>([^<]+)</\1>", text)}
    return fields["Modulus"], fields["D"], fields["Exponent"]


def key_id(modulus):
    raw = modulus.to_bytes((modulus.bit_length() + 7) // 8, "big")
    return hashlib.sha256(raw).hexdigest()[:16]


def sign(data, key_text):
    n, d, e = load_xml_key(key_text)
    size = (n.bit_length() + 7) // 8
    digest = DIGEST_INFO + hashlib.sha256(data).digest()
    block = b"\x00\x01" + b"\xff" * (size - len(digest) - 3) + b"\x00" + digest
    signature = pow(int.from_bytes(block, "big"), d, n).to_bytes(size, "big")
    assert pow(int.from_bytes(signature, "big"), e, n).to_bytes(size, "big") == block
    return (json.dumps({"format": "scex-manifest-signature/1", "algorithm": "RSA-PKCS1-v1_5-SHA256",
                        "keyId": key_id(n), "manifestSha256": hashlib.sha256(data).hexdigest(),
                        "signature": base64.b64encode(signature).decode("ascii")}, indent=2) + "\n").encode("utf-8")


if __name__ == "__main__":
    manifest, key = sys.argv[1], sys.argv[2]
    with open(manifest, "rb") as handle:
        data = handle.read()
    with open(key, encoding="utf-8-sig") as handle:
        text = handle.read()
    with open(manifest + ".sig", "wb") as handle:
        handle.write(sign(data, text))
