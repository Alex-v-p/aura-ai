# Aura work items

Every nontrivial repository write uses one YAML file under `active/`. Select it with `AURA_WORK_ITEM=AURA-NNNN`; a local untracked `.aura-work-item` containing the id is a fallback. Do not commit that pointer.

Create from `templates/work-item.yaml`. With no active item, the hook permits a single-file patch that adds only `active/AURA-NNNN.yaml`; all later writes require the new item to be valid and selected. Validate with:

```bash
python3 tooling/architecture/validate-work-item.py work-items/active/AURA-NNNN.yaml
```

Lifecycle: `draft` → `approved` → `in_progress` → `review` → `integrated`, or `blocked`. Move completed records to `archived/`. Implementation, review, and integration outputs must validate against their corresponding schema. Work-item scope may narrow path ownership but cannot widen it without an approved governance or contract change.
