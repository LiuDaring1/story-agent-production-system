import hashlib
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from story_evidence_store import externalize_evidence, resolve_evidence
from story_hash_cache import hash_cache_scope, sha256_file, validate_hash_cache
from story_production_v2 import directory_binding, current
from story_cli_output import compact

class EvidenceTests(unittest.TestCase):
    def test_external_members_deduplicated_bound_and_legacy_readable(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);tree=root/'frames';tree.mkdir()
            for i in range(70):(tree/f'{i}.png').write_bytes(str(i).encode())
            original=directory_binding(tree)
            value={'artifacts':[original,original]}
            stored=externalize_evidence(value,root/'evidence')
            self.assertEqual(len(list((root/'evidence').glob('*.json'))),1)
            self.assertEqual(resolve_evidence(stored),value)
            self.assertEqual(resolve_evidence(value),value)
            self.assertEqual(current(stored['artifacts'][0]),tree.resolve())
            ref=stored['artifacts'][0]['members_manifest']
            Path(ref['path']).write_text('[]')
            with self.assertRaisesRegex(ValueError,'Changed member evidence'):resolve_evidence(stored)

    def test_hash_transaction_reads_once_and_new_scope_rereads(self):
        with tempfile.TemporaryDirectory() as d:
            f=Path(d)/'x';f.write_bytes(b'original')
            original=Path.open;reads=[]
            def tracked(path,*a,**kw):
                if path==f and a and a[0]=='rb':reads.append(str(path))
                return original(path,*a,**kw)
            with patch.object(Path,'open',tracked):
                with hash_cache_scope():
                    sha256_file(f);sha256_file(f);validate_hash_cache()
                self.assertEqual(len(reads),1)
                with hash_cache_scope():sha256_file(f)
                self.assertEqual(len(reads),2)

    def test_replace_path_during_read_detected_even_if_old_fd_unchanged(self):
        with tempfile.TemporaryDirectory() as d:
            f=Path(d)/'x';f.write_bytes(b'old');replacement=Path(d)/'new';replacement.write_bytes(b'new')
            real=hashlib.sha256
            class Hash:
                def __init__(self):self.h=real()
                def update(self,data):
                    self.h.update(data)
                    if replacement.exists():os.replace(replacement,f)
                def hexdigest(self):return self.h.hexdigest()
            with patch('story_hash_cache.hashlib.sha256',Hash):
                with self.assertRaisesRegex(RuntimeError,'changed while hashing'):sha256_file(f)

    def test_mutation_after_hash_before_commit_is_rejected(self):
        with tempfile.TemporaryDirectory() as d:
            f=Path(d)/'x';f.write_bytes(b'old')
            with hash_cache_scope():
                sha256_file(f);f.write_bytes(b'new')
                with self.assertRaisesRegex(RuntimeError,'changed during validation'):validate_hash_cache()

    def test_directory_concurrent_member_creation_rejected(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);(root/'a').write_text('a')
            real=sha256_file
            def changing(path):
                answer=real(path);(root/'b').write_text('b');return answer
            with patch('story_production_v2.sha256_file',changing):
                with self.assertRaisesRegex(RuntimeError,'Directory changed'):directory_binding(root)

    def test_directory_addition_after_inventory_before_commit_rejected(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);(root/'a').write_text('a')
            with hash_cache_scope():
                directory_binding(root)
                (root/'later').write_text('new member')
                with self.assertRaisesRegex(RuntimeError,'Directory changed'):validate_hash_cache()

    def test_bounded_output_on_thousands_of_nested_records(self):
        v={'requests':{str(i):{'members':[{'path':'x'*200,'sha256':'a'*64}]*5000} for i in range(200)}}
        self.assertLess(len(json.dumps(compact(v))),6000)

if __name__=='__main__':unittest.main()
