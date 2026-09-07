import math
import re
from collections import deque
from typing import Iterable

from django.db import models


def normalize_rule_text(value: str) -> str:
    """保留中英文与数字，移除标点差异，便于稳定匹配。"""
    return re.sub(r'[^0-9a-zA-Z\u4e00-\u9fff]+', ' ', str(value or '').lower()).strip()


def extract_rule_keywords(value: str, limit: int = 20) -> list[str]:
    """从用户输入中提取可展示、可检索的关键词。"""
    pieces = re.split(r'[\s,，、;；。.!！?？:\n\r]+', str(value or ''))
    result = []
    for piece in pieces:
        keyword = piece.strip()
        if len(keyword) < 2 or keyword in result:
            continue
        result.append(keyword[:50])
        if len(result) >= limit:
            break
    return result


def _search_terms(value: str) -> set[str]:
    normalized = normalize_rule_text(value)
    terms = set(re.findall(r'[a-zA-Z0-9_]{2,}', normalized))
    for block in re.findall(r'[\u4e00-\u9fff]+', normalized):
        if len(block) <= 4:
            terms.add(block)
        for width in (2, 3, 4):
            if len(block) < width:
                continue
            terms.update(block[index:index + width] for index in range(len(block) - width + 1))
    return terms


def serialize_rule_snapshot(rule) -> dict:
    snapshot = {
        'id': rule.id,
        'title': rule.title,
        'content': rule.content,
        'keywords': rule.keywords or [],
        'project_id': rule.project_id,
        'project_name': rule.project.name if rule.project_id and rule.project else '',
        'module': rule.module,
        'business_domain': rule.business_domain,
        'entity_name': rule.entity_name,
        'attribute_name': rule.attribute_name,
        'state_values': rule.state_values or [],
        'relation_nodes': rule.relation_nodes or [],
        'test_strategies': rule.test_strategies or [],
        'risk_level': rule.risk_level,
        'rule_type': rule.rule_type,
        'applicable_conditions': rule.applicable_conditions,
        'expected_behavior': rule.expected_behavior,
        'exceptions': rule.exceptions,
        'version': rule.version,
    }
    node = getattr(rule, 'knowledge_node', None)
    if node:
        snapshot['knowledge_node_id'] = node.id
        snapshot['knowledge_neighbors'] = collect_knowledge_context([node.id])
    return snapshot


def _resolve_knowledge_anchor_node(project, anchor_name: str = '', parent_node_id=None):
    from .models import KnowledgeNode

    anchor_name = str(anchor_name or '').strip()
    node_qs = KnowledgeNode.objects.select_related('project', 'rule')
    if parent_node_id:
        parent_qs = node_qs.filter(id=parent_node_id)
        if project:
            parent_qs = parent_qs.filter(models.Q(project=project) | models.Q(project__isnull=True))
        else:
            parent_qs = parent_qs.filter(project__isnull=True)
        parent = parent_qs.first()
        if parent:
            return parent

    if not anchor_name:
        return None

    # A rule node may share the same display name as its business-domain
    # anchor.  Only unbound domain nodes are valid implicit anchors; otherwise
    # syncing a rule can create a self-loop relation to its own node.
    anchor_qs = node_qs.filter(
        name=anchor_name,
        type='domain',
        rule__isnull=True,
    )
    if project:
        anchor_qs = anchor_qs.filter(models.Q(project=project) | models.Q(project__isnull=True))
    else:
        anchor_qs = anchor_qs.filter(project__isnull=True)

    return anchor_qs.order_by('type', 'id').first()


def merge_duplicate_domain_nodes(project=None, name: str = '') -> int:
    """Merge same-name root nodes inside the same project scope."""
    from .models import KnowledgeNode, KnowledgeRelation

    queryset = KnowledgeNode.objects.filter(type='domain', rule__isnull=True)
    if project is not None:
        queryset = queryset.filter(project=project)
    if name:
        queryset = queryset.filter(name=str(name).strip())

    groups = {}
    for node in queryset.order_by('project_id', 'name', 'id'):
        key = (node.project_id, normalize_rule_text(node.name))
        if not key[1]:
            continue
        groups.setdefault(key, []).append(node)

    merged_count = 0
    for nodes in groups.values():
        if len(nodes) < 2:
            continue
        keeper = nodes[0]
        duplicate_ids = [node.id for node in nodes[1:]]
        for duplicate_id in duplicate_ids:
            outgoing = KnowledgeRelation.objects.filter(source_id=duplicate_id)
            for relation in outgoing:
                if relation.target_id == keeper.id:
                    relation.delete()
                    continue
                KnowledgeRelation.objects.get_or_create(
                    source_id=keeper.id,
                    target_id=relation.target_id,
                    relation_type=relation.relation_type,
                )
                relation.delete()

            incoming = KnowledgeRelation.objects.filter(target_id=duplicate_id)
            for relation in incoming:
                if relation.source_id == keeper.id:
                    relation.delete()
                    continue
                KnowledgeRelation.objects.get_or_create(
                    source_id=relation.source_id,
                    target_id=keeper.id,
                    relation_type=relation.relation_type,
                )
                relation.delete()

        KnowledgeNode.objects.filter(id__in=duplicate_ids).delete()
        merged_count += len(duplicate_ids)

    return merged_count


def sync_rule_to_knowledge_node(rule, parent_node=None, root_name: str = ''):
    """Ensure a business rule is represented as a node under a selectable anchor node."""
    from .models import KnowledgeNode, KnowledgeRelation

    if getattr(rule, 'status', '') == 'deprecated':
        KnowledgeNode.objects.filter(rule=rule).delete()
        return None

    # Updating a rule must keep its existing topology placement. Resolving the
    # anchor from the (possibly edited) title would otherwise create a new
    # domain node every time a rule name changes.
    existing_node = KnowledgeNode.objects.filter(rule=rule).first()
    anchor_node = parent_node
    if anchor_node is None and existing_node:
        existing_relation = KnowledgeRelation.objects.filter(
            target=existing_node,
            relation_type='contains',
            source__type='domain',
        ).select_related('source').order_by('id').first()
        candidate = existing_relation.source if existing_relation else None
        if candidate and (
            rule.project_id is None
            or candidate.project_id is None
            or candidate.project_id == rule.project_id
        ):
            # A global rule may intentionally live under a project knowledge
            # file, so the existing graph relation is the source of truth for
            # placement in that case as well.
            anchor_node = candidate

    anchor_name = str(root_name or rule.title or rule.business_domain or rule.module or '未分组业务').strip()
    if anchor_node is None and not existing_node:
        merge_duplicate_domain_nodes(project=rule.project, name=anchor_name)
        anchor_node = _resolve_knowledge_anchor_node(rule.project, anchor_name)

    # A rule update must never create a new knowledge-file root merely because
    # its existing relation is missing or temporarily inconsistent. Preserve
    # the current rule node in place; root creation is only valid for a new
    # rule that has no topology node yet.
    if anchor_node is None and existing_node:
        rule_name = str(rule.content or rule.title or '').strip()
        if len(rule_name) > 36:
            rule_name = f'{rule_name[:36]}...'
        if not rule_name:
            rule_name = anchor_name
        changed = []
        if existing_node.name != rule_name:
            existing_node.name = rule_name
            changed.append('name')
        if existing_node.content != rule.content:
            existing_node.content = rule.content
            changed.append('content')
        if existing_node.project_id != rule.project_id:
            existing_node.project_id = rule.project_id
            changed.append('project')
        if changed:
            existing_node.save(update_fields=[*changed, 'updated_at'])
        return existing_node

    if not anchor_node:
        anchor_node = KnowledgeNode.objects.create(
            name=anchor_name,
            type='domain',
            content=f'{anchor_name}业务知识一级节点',
            project=rule.project,
        )

    rule_name = str(rule.content or rule.title or '').strip()
    if len(rule_name) > 36:
        rule_name = f'{rule_name[:36]}...'
    if not rule_name:
        rule_name = anchor_name

    node, _created = KnowledgeNode.objects.update_or_create(
        rule=rule,
        defaults={
            'name': rule_name,
            'type': 'rule',
            'content': rule.content,
            'project': rule.project,
        }
    )
    KnowledgeRelation.objects.get_or_create(
        source=anchor_node,
        target=node,
        relation_type='contains',
    )
    return node


def collect_knowledge_context(node_ids: Iterable[int], depth: int = 2, max_nodes: int = 30) -> list[dict]:
    """Recursively collect upstream and downstream rules around selected graph nodes."""
    from .models import KnowledgeNode, KnowledgeRelation

    start_ids = []
    for node_id in node_ids or []:
        try:
            parsed = int(node_id)
        except (TypeError, ValueError):
            continue
        if parsed > 0 and parsed not in start_ids:
            start_ids.append(parsed)

    if not start_ids:
        return []

    visited = set(start_ids)
    queue = deque((node_id, 0) for node_id in start_ids)
    relation_paths = []

    while queue and len(visited) < max_nodes:
        current_id, level = queue.popleft()
        if level >= depth:
            continue
        relations = KnowledgeRelation.objects.filter(
            models.Q(source_id=current_id) | models.Q(target_id=current_id)
        ).select_related('source', 'target')[:200]
        for relation in relations:
            neighbor_id = relation.target_id if relation.source_id == current_id else relation.source_id
            direction = 'downstream' if relation.source_id == current_id else 'upstream'
            relation_paths.append({
                'source': relation.source_id,
                'source_name': relation.source.name,
                'target': relation.target_id,
                'target_name': relation.target.name,
                'relation': relation.relation_type,
                'direction': direction,
                'depth': level + 1,
            })
            if neighbor_id not in visited and len(visited) < max_nodes:
                visited.add(neighbor_id)
                queue.append((neighbor_id, level + 1))

    nodes = {
        node.id: node
        for node in KnowledgeNode.objects.filter(id__in=visited).select_related('project', 'rule')
    }
    result = []
    for node_id in sorted(visited):
        node = nodes.get(node_id)
        if not node:
            continue
        result.append({
            'id': node.id,
            'name': node.name,
            'type': node.type,
            'content': node.content,
            'project_id': node.project_id,
            'project_name': node.project.name if node.project_id and node.project else '',
            'rule_id': node.rule_id,
            'is_selected': node.id in start_ids,
            'relations': [
                item for item in relation_paths
                if item['source'] == node.id or item['target'] == node.id
            ][:12],
        })
    return result


def score_business_rule(rule, query: str, project_id=None, module: str = '') -> float:
    """关键词、范围和历史采纳效果的混合评分，范围为0到1。"""
    query = str(query or '')[:10000]
    query_normalized = normalize_rule_text(query)
    if not query_normalized:
        return 0

    def field(name, default=''):
        return getattr(rule, name, default)

    rule_text = ' '.join([
        field('title'),
        field('content'),
        ' '.join(field('keywords', []) or []),
        field('module'),
        field('business_domain'),
        field('entity_name'),
        field('attribute_name'),
        ' '.join(str(item) for item in (field('state_values', []) or [])),
        ' '.join(str(item) for item in (field('relation_nodes', []) or [])),
        ' '.join(str(item) for item in (field('test_strategies', []) or [])),
        field('applicable_conditions'),
        field('expected_behavior'),
        field('exceptions'),
    ])
    rule_normalized = normalize_rule_text(rule_text)
    query_terms = _search_terms(query_normalized)
    rule_terms = _search_terms(rule_normalized)
    overlap = len(query_terms & rule_terms)
    dice_score = (2 * overlap / (len(query_terms) + len(rule_terms))) if query_terms and rule_terms else 0

    score = min(dice_score * 1.8, 0.68)
    if rule_normalized and rule_normalized in query_normalized:
        score += 0.18
    elif query_normalized and query_normalized in rule_normalized:
        score += 0.12

    rule_project_id = field('project_id', None)
    if project_id and rule_project_id == int(project_id):
        score += 0.12
    elif rule_project_id is None:
        score += 0.03

    module_normalized = normalize_rule_text(module)
    rule_module_normalized = normalize_rule_text(field('module'))
    if module_normalized and rule_module_normalized:
        if module_normalized in rule_module_normalized or rule_module_normalized in module_normalized:
            score += 0.08

    usage_count = max(int(field('usage_count', 0) or 0), 0)
    accepted_count = max(int(field('accepted_count', 0) or 0), 0)
    if usage_count:
        score += min((accepted_count / usage_count) * 0.06, 0.06)
    score += min(math.log1p(accepted_count) * 0.01, 0.03)
    return round(min(score, 1.0), 4)


def recommend_business_rules(
    rules: Iterable,
    query: str,
    project_id=None,
    module: str = '',
    limit: int = 8,
    minimum_score: float = 0.08,
) -> list[tuple[object, float]]:
    scored = []
    for rule in rules:
        score = score_business_rule(rule, query, project_id=project_id, module=module)
        if score >= minimum_score:
            scored.append((rule, score))
    scored.sort(key=lambda item: (-item[1], -int(item[0].accepted_count or 0), item[0].title))
    return scored[:max(1, min(int(limit or 8), 20))]
