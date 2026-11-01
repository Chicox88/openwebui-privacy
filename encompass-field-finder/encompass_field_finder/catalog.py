"""Catalog of Encompass settings endpoints scanned by :class:`GenericSettingsSource`.

Every entry below was taken from the ICE Developer Connect API reference
(developer.icemortgagetechnology.com) unless it is marked ``"documented": False``.
Undocumented entries are tried and silently reported as "not available" if the instance
answers 404, so they are safe to keep.

You can add / override entries without touching code: put a JSON or YAML file with a top-level
``entries`` list (same keys as below) and pass it with ``--catalog my_catalog.yaml``.
An entry with the same ``key`` as a built-in one replaces it; ``"enabled": false`` disables it.

Entry keys
----------
key, category, item_type      identifiers / grouping shown in reports
location                      UI hint; ``{name}`` is replaced with the item's name
list.path / list.params       collection endpoint
list.items_key                key holding the array (auto-detected if omitted)
list.paginate                 {"start_param", "limit_param", "start", "limit", "max_pages"}
item_is_name                  the list returns bare strings (names) instead of objects
id_keys / name_keys           which item properties identify / name an item
detail.path / detail.params   per-item endpoint (``{id}`` / any item key is substituted)
decode_base64                 also scan base64 encoded string values (CDOs)
contract_paths                values are loan-contract JSON: match by field contract path too
update                        {"method", "path", "params", "body": "item"|"list", "strip": [...]}
                              enables automatic replace/remove for this entry
"""

from __future__ import annotations

import copy
import json
import os
from typing import Any, Dict, List, Optional

_PAGE_0 = {"start_param": "start", "limit_param": "limit", "start": 0, "limit": 1000, "max_pages": 50}
_PAGE_1 = {"start_param": "start", "limit_param": "limit", "start": 1, "limit": 1000, "max_pages": 50}

DEFAULT_CATALOG: List[Dict[str, Any]] = [
    # ---------------------------------------------------------------- conditions
    {
        "key": "enhanced_condition_templates",
        "category": "Enhanced Conditions",
        "item_type": "Enhanced Condition Template",
        "location": "Encompass > Settings > eFolder Setup > Enhanced Conditions > Condition Templates > '{name}'",
        "list": {"path": "/encompass/v3/settings/loan/conditions/templates",
                 "params": {"view": "full", "context": "Settings"}, "paginate": _PAGE_1},
        "id_keys": ["id"], "name_keys": ["title", "internalId", "id"],
        "detail": {"path": "/encompass/v3/settings/loan/conditions/templates/{id}", "params": {"view": "full"}},
        "update": {"method": "PATCH", "path": "/encompass/v3/settings/loan/conditions/templates",
                   "params": {"action": "Update"}, "body": "list",
                   "strip": ["createdDate", "createdBy", "lastModifiedDate", "lastModifiedBy"]},
    },
    {
        "key": "enhanced_condition_types",
        "category": "Enhanced Conditions",
        "item_type": "Enhanced Condition Type",
        "location": "Encompass > Settings > eFolder Setup > Enhanced Conditions > Condition Types > '{name}'",
        "list": {"path": "/encompass/v3/settings/loan/conditions/types"},
        "id_keys": ["id"], "name_keys": ["title", "id"],
        "detail": {"path": "/encompass/v3/settings/loan/conditions/types/{id}"},
        "update": {"method": "PATCH", "path": "/encompass/v3/settings/loan/conditions/types",
                   "params": {"action": "Update"}, "body": "list",
                   "strip": ["createdDate", "createdBy", "lastModifiedDate", "lastModifiedBy"]},
    },
    {
        "key": "enhanced_condition_sets",
        "category": "Enhanced Conditions",
        "item_type": "Enhanced Condition Set",
        "location": "Encompass > Settings > eFolder Setup > Enhanced Conditions > Condition Sets > '{name}'",
        "list": {"path": "/encompass/v3/settings/loan/conditions/sets"},
        "id_keys": ["id"], "name_keys": ["name", "title", "id"],
        "detail": {"path": "/encompass/v3/settings/loan/conditions/sets/{id}"},
    },
    # ---------------------------------------------------------------- eFolder
    {
        "key": "efolder_documents",
        "category": "eFolder Documents",
        "item_type": "eFolder Document Setup",
        "location": "Encompass > Settings > eFolder Setup > Documents > '{name}'",
        "list": {"path": "/encompass/v3/settings/efolder/documents", "params": {"view": "Detail"},
                 "paginate": _PAGE_0},
        "id_keys": ["id"], "name_keys": ["name", "id"],
    },
    {
        "key": "efolder_document_groups",
        "category": "eFolder Documents",
        "item_type": "eFolder Document Group",
        "location": "Encompass > Settings > eFolder Setup > Document Groups > '{name}'",
        "list": {"path": "/encompass/v3/settings/eFolder/documentGroup"},
        "id_keys": ["id"], "name_keys": ["name", "id"],
    },
    {
        "key": "efolder_stacking_templates",
        "category": "eFolder Documents",
        "item_type": "Document Stacking Template",
        "location": "Encompass > Settings > eFolder Setup > Stacking Order > '{name}'",
        "list": {"path": "/encompass/v3/settings/eFolder/documentStackingTemplates",
                 "paginate": {**_PAGE_0, "limit": 100}},
        "id_keys": ["id"], "name_keys": ["name", "id"],
        "detail": {"path": "/encompass/v3/settings/eFolder/documentStackingTemplates/{id}"},
    },
    # ---------------------------------------------------------------- forms
    {
        "key": "print_form_groups",
        "category": "Print Forms",
        "item_type": "Print Form Group",
        "location": "Encompass > Settings > Forms > Print Form Groups > '{name}'",
        "list": {"path": "/encompass/v3/settings/loan/printFormGroups"},
        "id_keys": ["entityPath", "path", "entityId", "id"], "name_keys": ["entityName", "name"],
        "detail": {"path": "/encompass/v3/settings/loan/printFormGroupDetail", "params": {"path": "{id}"}},
    },
    {
        "key": "custom_print_forms",
        "category": "Print Forms",
        "item_type": "Custom Print Form (name only)",
        "location": "Encompass > Settings > Forms > Custom Print Forms > '{name}'",
        "list": {"path": "/encompass/v3/settings/loan/customPrintForms",
                 "params": {"level": "All", "path": "Public:\\"}, "items_key": "contents",
                 "paginate": {**_PAGE_0, "limit": 1000}},
        "id_keys": ["entityPath", "entityId"], "name_keys": ["entityName", "fileName"],
        "notes": "The API returns the form list only. Field mappings inside custom print forms "
                 "(Word/PDF) are found by the offline export scanner.",
    },
    # ---------------------------------------------------------------- loan setup
    {
        "key": "milestones",
        "category": "Milestones",
        "item_type": "Milestone",
        "location": "Encompass > Settings > Company/User Setup > Milestones > '{name}'",
        "list": {"path": "/encompass/v3/settings/milestones"},
        "id_keys": ["id"], "name_keys": ["name", "id"],
        "detail": {"path": "/encompass/v3/settings/milestones/{id}"},
    },
    {
        "key": "personas",
        "category": "Personas",
        "item_type": "Persona",
        "location": "Encompass > Settings > Company/User Setup > Personas > '{name}'",
        "list": {"path": "/encompass/v3/settings/personas"},
        "id_keys": ["id"], "name_keys": ["name", "id"],
        "detail": {"path": "/encompass/v3/settings/personas/{id}"},
    },
    {
        "key": "roles",
        "category": "Roles",
        "item_type": "Role",
        "location": "Encompass > Settings > Company/User Setup > Roles > '{name}'",
        "list": {"path": "/encompass/v3/settings/roles"},
        "id_keys": ["id", "roleId"], "name_keys": ["roleName", "name", "id"],
        "detail": {"path": "/encompass/v3/settings/roles/{id}"},
    },
    {
        "key": "hmda_profiles",
        "category": "Loan Setup",
        "item_type": "HMDA Profile",
        "location": "Encompass > Settings > Loan Setup > HMDA Profiles > '{name}'",
        "list": {"path": "/encompass/v3/settings/loan/hmdaProfiles"},
        "id_keys": ["id", "profileId"], "name_keys": ["name", "profileName", "id"],
        "detail": {"path": "/encompass/v3/settings/loan/hmdaProfiles/{id}"},
    },
    {
        "key": "disclosure_tracking",
        "category": "Loan Setup",
        "item_type": "Disclosure Tracking Settings",
        "location": "Encompass > Settings > Loan Setup > Disclosure Tracking",
        "list": {"path": "/encompass/v3/settings/loan/disclosureTracking"},
        "name_keys": [],
    },
    {
        "key": "urla_policy",
        "category": "Loan Setup",
        "item_type": "URLA Configuration",
        "location": "Encompass > Settings > Policies > URLA",
        "list": {"path": "/encompass/v3/settings/policies/urla"},
        "name_keys": [],
    },
    {
        "key": "itemization_fee_management",
        "category": "Fees",
        "item_type": "Itemization Fee Management",
        "location": "Encompass > Settings > Tables and Fees > Itemization Fee Management",
        "list": {"path": "/encompass/v3/settings/fees/itemization"},
        "name_keys": ["name", "lineNumber", "id"],
    },
    # ---------------------------------------------------------------- secondary / tpo
    {
        "key": "investor_templates",
        "category": "Secondary",
        "item_type": "Investor Template",
        "location": "Encompass > Settings > Secondary Setup > Investor Templates > '{name}'",
        "list": {"path": "/encompass/v3/settings/secondary/investorTemplates"},
        "id_keys": ["id", "templateId"], "name_keys": ["templateName", "name", "id"],
        "detail": {"path": "/encompass/v3/settings/secondary/investorTemplates/{id}"},
    },
    {
        "key": "funding_templates",
        "category": "Secondary",
        "item_type": "Funding Template",
        "location": "Encompass > Settings > Secondary Setup > Funding Templates > '{name}'",
        "list": {"path": "/encompass/v3/settings/secondary/fundingTemplates"},
        "id_keys": ["id"], "name_keys": ["templateName", "name", "id"],
    },
    {
        "key": "transcript_templates",
        "category": "Templates",
        "item_type": "Transcript of Tax Template",
        "location": "Encompass > Settings > Docs Setup > Transcript Templates > '{name}'",
        "list": {"path": "/encompass/v3/settings/templates/transcriptRequests"},
        "id_keys": ["id", "templateId"], "name_keys": ["templateName", "name", "id"],
        "detail": {"path": "/encompass/v3/settings/templates/transcriptRequests/{id}"},
    },
    {
        "key": "tpo_custom_field_definitions",
        "category": "TPO",
        "item_type": "TPO Custom Field Definition",
        "location": "Encompass > Settings > External Company Setup > TPO Custom Fields",
        "list": {"path": "/encompass/v3/settings/externalOrganizations/tpoCustomFieldDefinitions"},
        "id_keys": ["id", "fieldId"], "name_keys": ["description", "fieldName", "id"],
    },
    {
        "key": "business_contact_custom_fields",
        "category": "Contacts",
        "item_type": "Business Contact Custom Field",
        "location": "Encompass > Settings > Contact Setup > Business Contact Custom Fields",
        "list": {"path": "/encompass/v3/settings/contacts/businessContacts/customFields"},
        "id_keys": ["id", "fieldId"], "name_keys": ["description", "label", "id"],
    },
    {
        "key": "business_contact_category_fields",
        "category": "Contacts",
        "item_type": "Business Contact Category Field",
        "location": "Encompass > Settings > Contact Setup > Business Contact Category Fields",
        "list": {"path": "/encompass/v3/settings/contacts/businessContacts/customCategoryFields"},
        "id_keys": ["id", "fieldId"], "name_keys": ["description", "label", "id"],
    },
    # ---------------------------------------------------------------- workflow / integrations
    {
        "key": "task_settings",
        "category": "Workflow",
        "item_type": "Task Settings",
        "location": "Encompass > Settings > Task Setup",
        "list": {"path": "/workflow/v1/settings/task"},
        "name_keys": [],
    },
    {
        "key": "task_templates",
        "category": "Workflow",
        "item_type": "Task Template",
        "location": "Encompass > Settings > Task Setup > Task Templates > '{name}'",
        "list": {"path": "/workflow/v1/templates/task/items"},
        "id_keys": ["id"], "name_keys": ["name", "title", "id"],
        "detail": {"path": "/workflow/v1/templates/task/items/{id}"},
        "update": {"method": "PATCH", "path": "/workflow/v1/templates/task/items/{id}", "body": "item",
                   "strip": ["id", "createdBy", "createdDate", "lastModifiedBy", "lastModifiedDate"]},
    },
    {
        "key": "webhook_subscriptions",
        "category": "Webhooks",
        "item_type": "Webhook Subscription",
        "location": "Developer Connect webhook subscription '{name}' (API only, no UI)",
        "list": {"path": "/webhook/v1/subscriptions"},
        "id_keys": ["subscriptionId"], "name_keys": ["endpoint", "subscriptionId"],
        "contract_paths": True,
        "update": {"method": "PUT", "path": "/webhook/v1/subscriptions/{id}", "body": "item",
                   "strip": ["subscriptionId", "clientId", "instanceId"]},
    },
    {
        "key": "global_custom_data_objects",
        "category": "Custom Data Objects",
        "item_type": "Global Custom Data Object (CDO)",
        "location": "Global custom data object '{name}' (used by plugins/codebase/web tools)",
        "list": {"path": "/encompass/v1/company/customObjects"},
        "item_is_name": True,
        "id_keys": ["name"], "name_keys": ["name"],
        "detail": {"path": "/encompass/v1/company/customObjects/{id}"},
        "decode_base64": True,
        "update": {"method": "PUT", "path": "/encompass/v1/company/customObjects/{id}", "body": "item"},
    },
    {
        "key": "pipeline_views",
        "category": "Pipeline Views",
        "item_type": "Pipeline View (API user's own)",
        "location": "Encompass > Pipeline > Views > '{name}' (views of the API user only)",
        "documented": False,
        "list": {"path": "/encompass/v3/users/me/views/pipelineViews"},
        "id_keys": ["id"], "name_keys": ["name", "id"],
        "update": {"method": "PATCH", "path": "/encompass/v3/users/me/views/pipelineViews/{id}",
                   "body": "item", "strip": ["id"]},
    },
    # ---------------------------------------------------------------- consumer connect / opportunities
    {
        "key": "loan_opportunity_letter_templates",
        "category": "Letter / Email Templates",
        "item_type": "Loan Opportunity Letter Template",
        "location": "Loan Opportunity (Consumer Connect) settings > Letter Templates > '{name}'",
        "list": {"path": "/loanOpportunity/v1/settings/letterTemplates"},
        "id_keys": ["id", "templateId"], "name_keys": ["name", "templateName", "id"],
        "detail": {"path": "/loanOpportunity/v1/settings/letterTemplates/{id}"},
        "decode_base64": True,
        "update": {"method": "PUT", "path": "/loanOpportunity/v1/settings/letterTemplates/{id}", "body": "item"},
    },
    {
        "key": "loan_opportunity_email_templates",
        "category": "Letter / Email Templates",
        "item_type": "Loan Opportunity Email Template",
        "location": "Loan Opportunity (Consumer Connect) settings > Email Templates > '{name}'",
        "list": {"path": "/loanOpportunity/v1/settings/emailTemplates"},
        "id_keys": ["id", "templateId"], "name_keys": ["name", "templateName", "id"],
        "detail": {"path": "/loanOpportunity/v1/settings/emailTemplates/{id}"},
        "update": {"method": "PUT", "path": "/loanOpportunity/v1/settings/emailTemplates/{id}", "body": "item"},
    },
    # ---------------------------------------------------------------- undocumented probes
    # These are guesses at endpoints ICE has not published. They are tried and reported as
    # "not available" when the instance answers 404. Remove them or add your own via --catalog.
    {
        "key": "business_rules_probe",
        "category": "Business Rules",
        "item_type": "Business Rule",
        "location": "Encompass > Settings > Business Rules",
        "documented": False,
        "enabled": False,
        "list": {"path": "/encompass/v3/settings/businessRules"},
        "name_keys": ["name", "ruleName", "id"],
    },
    {
        "key": "ddm_field_rules_probe",
        "category": "Dynamic Data Management",
        "item_type": "DDM Field Rule",
        "location": "Encompass > Settings > Dynamic Data Management > Field Rules",
        "documented": False,
        "enabled": False,
        "list": {"path": "/encompass/v3/settings/ddm/fieldRules"},
        "name_keys": ["name", "ruleName", "id"],
    },
]


def load_catalog(path: Optional[str] = None, include_undocumented: bool = False) -> List[Dict[str, Any]]:
    """Return the effective catalog (built-in entries merged with an optional user file)."""
    entries = {e["key"]: copy.deepcopy(e) for e in DEFAULT_CATALOG}
    if path:
        with open(path, "r", encoding="utf-8") as fh:
            text = fh.read()
        if os.path.splitext(path)[1].lower() in (".yaml", ".yml"):
            import yaml  # optional dependency

            data = yaml.safe_load(text)
        else:
            data = json.loads(text)
        for e in (data or {}).get("entries", []):
            entries[e["key"]] = {**entries.get(e["key"], {}), **e}
    out = []
    for e in entries.values():
        enabled = e.get("enabled", True)
        if include_undocumented and e.get("documented", True) is False and "enabled" in e:
            enabled = True
        if enabled:
            out.append(e)
    return out
