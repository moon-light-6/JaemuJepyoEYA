"""v3 문서의 출처 메타데이터를 유지해 청킹한다.

현 단계에서는 v2 분할기를 재사용한다. 표·본문 전용 분할은 별도 검증이 필요하다.
"""
from scripts.processing.build_xml_chunks_v2 import build_xml_chunks

__all__ = ['build_xml_chunks']
