import json
import struct
import pytest
from jshi.voice.volc import unpack_asr

@pytest.mark.parametrize('payload,detail', [({},'连续8秒'),({'msg':'waiting audio timed out'},'waiting audio timed out'),({'message':'provider detail'},'provider detail')])
def test_packet_timeout_retains_provider_detail_or_explains_code(payload,detail):
    body=json.dumps(payload).encode()
    frame=bytes((0x11,0xf0,0x10,0))+struct.pack('>II',45000081,len(body))+body
    with pytest.raises(RuntimeError,match='45000081.*'+detail):unpack_asr(frame)
