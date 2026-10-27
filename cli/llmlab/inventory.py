"""Owner-side inventory. Bounded model inspection stays in the user's process."""
from __future__ import annotations

from dataclasses import asdict
import hashlib
import os
from pathlib import Path

from backend.registry.agent_reports import MAX_MODELS
from backend.registry.discovery.rules import scan_tree
from backend.registry.discovery.facts import read_model
from backend.registry.discovery.fingerprint import fingerprint_file


def inventory_fingerprint(candidate):
    """Bounded content samples distinguish same-size checkpoints, independent of location.

    This is a relocation hint, not a cryptographic proof of whole-file equality.
    At most 128 shards × 128 KiB are read. Larger/unreadable models match by inode only.
    """
    if candidate.kind == 'gguf':
        return fingerprint_file(candidate.path)
    if len(candidate.weights) > 128:
        return None
    digest = hashlib.sha256()
    for weight in sorted(candidate.weights, key=lambda w: str(w.path)):
        sample = fingerprint_file(weight.path, chunk=65536)
        if sample.startswith('unreadable:'):
            return None
        digest.update(str(weight.path.relative_to(candidate.path)).encode())
        digest.update(b'\0' + sample.encode() + b'\n')
    for name in ('config.json', 'model_index.json'):
        path = candidate.path / name
        if path.is_file():
            sample = fingerprint_file(path, chunk=65536)
            if sample.startswith('unreadable:'):
                return None
            digest.update(name.encode() + b'\0' + sample.encode())
    return f'{candidate.size_bytes}:{digest.hexdigest()}'


def scan_models(roots: tuple[str, ...]) -> tuple[list[dict], list[dict], list[str]]:
    models, builds, problems = [], [], []
    seen = set()
    uid = os.getuid()
    for root in roots:
        root = Path(root).absolute()
        try:
            with (root / '.llmlab-ignore').open() as handle:
                ignore = handle.read(65536).splitlines()
            ignore = [x.strip() for x in ignore if x.strip() and not x.lstrip().startswith('#')]
        except OSError:
            ignore = []
        result = scan_tree(root, ignore_globs=ignore)
        for skip in result.skipped:
            if skip.reason in {'unreadable', 'max_depth', 'max_entries'}:
                problems.append(f'model_walk_truncated:{skip.reason}:{skip.path}')
        for candidate in result.models:
            path = str(candidate.path)
            if path in seen:
                continue
            try:
                info = candidate.path.stat()
                if info.st_uid != uid or any(w.path.stat().st_uid != uid for w in candidate.weights):
                    continue
            except OSError:
                problems.append(f'model_walk_truncated:unreadable:{path}')
                continue
            if len(models) >= MAX_MODELS:
                problems.append('model_candidates_capped:200')
                break
            seen.add(path)
            facts = read_model(candidate)
            # Raw config/header values are unnecessary for the management UI.
            metadata = {k: v for k, v in asdict(facts).items() if k != 'raw' and v is not None}
            models.append({
                'path': path, 'format': candidate.kind, 'size_bytes': candidate.size_bytes,
                'architecture': facts.architecture, 'model_type': facts.task,
                'suggested_name': facts.name or candidate.path.stem,
                'fingerprint': inventory_fingerprint(candidate),
                'device': info.st_dev, 'inode': info.st_ino, 'mtime': info.st_mtime,
                'facts': metadata,
            })
        for candidate in result.builds:
            try:
                if candidate.binary.stat().st_uid != uid:
                    continue
            except OSError:
                continue
            builds.append({'engine': candidate.framework, 'name': candidate.framework,
                           'found_as': 'executable', 'version': candidate.version,
                           'path': str(candidate.binary), 'env': None})
    return models, builds, problems
