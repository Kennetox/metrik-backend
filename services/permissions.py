from __future__ import annotations

from copy import deepcopy
from typing import Any, Dict, List, Optional


ROLE_KEYS = ["Administrador", "Supervisor", "Vendedor", "Auditor"]


def _role_flags(**overrides: bool) -> Dict[str, bool]:
    flags = {role: False for role in ROLE_KEYS}
    for role, value in overrides.items():
        if role in flags:
            flags[role] = bool(value)
    return flags


DEFAULT_ROLE_PERMISSION_MODULES: List[Dict[str, Any]] = [
    {
        "id": "dashboard",
        "label": "Dashboard",
        "description": "Indicadores generales del negocio.",
        "roles": _role_flags(
            Administrador=True, Supervisor=True, Vendedor=True, Auditor=True
        ),
        "actions": [
            {
                "id": "dashboard.view",
                "label": "Ver dashboard",
                "description": "Permite acceder al panel principal.",
                "roles": _role_flags(
                    Administrador=True, Supervisor=True, Vendedor=True, Auditor=True
                ),
            }
        ],
    },
    {
        "id": "pos",
        "label": "POS / Caja",
        "description": "Punto de venta y operaciones de caja.",
        "roles": _role_flags(Administrador=True, Supervisor=True, Vendedor=True),
        "actions": [
            {
                "id": "pos.sales",
                "label": "Gestionar ventas",
                "description": "Crear, listar y editar ventas.",
                "roles": _role_flags(Administrador=True, Supervisor=True, Vendedor=True),
            },
            {
                "id": "pos.returns",
                "label": "Devoluciones",
                "description": "Registrar y consultar devoluciones.",
                "roles": _role_flags(Administrador=True, Supervisor=True, Vendedor=True),
            },
            {
                "id": "pos.customers",
                "label": "Clientes POS",
                "description": "Crear y administrar clientes.",
                "roles": _role_flags(Administrador=True, Supervisor=True, Vendedor=True),
            },
            {
                "id": "pos.closures",
                "label": "Cierres de caja",
                "description": "Gestionar cierres e informes diarios.",
                "roles": _role_flags(Administrador=True, Supervisor=True, Vendedor=True),
            },
        ],
    },
    {
        "id": "documents",
        "label": "Documentos",
        "description": "Separados y documentos relacionados.",
        "roles": _role_flags(Administrador=True, Supervisor=True, Vendedor=True),
        "actions": [
            {
                "id": "documents.separated_orders",
                "label": "Separados",
                "roles": _role_flags(Administrador=True, Supervisor=True, Vendedor=True),
            }
        ],
    },
    {
        "id": "products",
        "label": "Productos",
        "description": "Catálogo e inventario.",
        "roles": _role_flags(Administrador=True, Supervisor=True),
        "actions": [
            {
                "id": "products.manage",
                "label": "Administrar productos",
                "roles": _role_flags(Administrador=True, Supervisor=True),
            },
            {
                "id": "products.labels",
                "label": "Exportar etiquetas",
                "roles": _role_flags(Administrador=True, Supervisor=True),
            },
        ],
    },
    {
        "id": "reports",
        "label": "Reportes",
        "description": "Reportes financieros y de inventario.",
        "roles": _role_flags(
            Administrador=True, Supervisor=True, Vendedor=True, Auditor=True
        ),
        "actions": [
            {
                "id": "reports.view",
                "label": "Ver reportes",
                "roles": _role_flags(
                    Administrador=True, Supervisor=True, Vendedor=True, Auditor=True
                ),
            }
        ],
    },
    {
        "id": "settings",
        "label": "Configuración",
        "description": "Preferencias del POS, SMTP y otros ajustes.",
        "roles": _role_flags(Administrador=True),
        "actions": [
            {
                "id": "settings.manage",
                "label": "Configurar POS",
                "roles": _role_flags(Administrador=True),
            },
            {
                "id": "settings.payment_methods",
                "label": "Métodos de pago",
                "roles": _role_flags(Administrador=True),
            },
        ],
    },
    {
        "id": "users",
        "label": "Usuarios",
        "description": "Gestión e invitación de usuarios POS.",
        "roles": _role_flags(Administrador=True, Supervisor=True),
        "actions": [
            {
                "id": "users.manage",
                "label": "Administrar usuarios",
                "roles": _role_flags(Administrador=True, Supervisor=True),
            },
            {
                "id": "users.invite",
                "label": "Invitar usuarios",
                "roles": _role_flags(Administrador=True, Supervisor=True),
            },
            {
                "id": "stations.manage",
                "label": "Estaciones POS",
                "description": "Administrar estaciones y PINs de caja.",
                "roles": _role_flags(Administrador=True),
            },
        ],
    },
]


def get_default_permissions() -> List[Dict[str, Any]]:
    return deepcopy(DEFAULT_ROLE_PERMISSION_MODULES)


def _index_by_id(items: List[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    return {item.get("id"): item for item in items if item.get("id")}


def _merge_roles(
    default_roles: Dict[str, bool], override_roles: Optional[Dict[str, Any]]
) -> Dict[str, bool]:
    merged = dict(default_roles)
    if not override_roles:
        return merged
    for role in ROLE_KEYS:
        if role in override_roles:
            merged[role] = bool(override_roles[role])
    return merged


def _merge_actions(
    default_actions: List[Dict[str, Any]], override_actions: Optional[List[Dict[str, Any]]]
) -> List[Dict[str, Any]]:
    if not override_actions:
        return default_actions
    override_map = _index_by_id(override_actions)
    merged_actions = []
    for action in default_actions:
        override = override_map.get(action["id"])
        action_copy = dict(action)
        if override:
            action_copy["roles"] = _merge_roles(
                action["roles"], override.get("roles")
            )
        merged_actions.append(action_copy)
    return merged_actions


def ensure_permissions(
    modules: Optional[List[Dict[str, Any]]],
) -> List[Dict[str, Any]]:
    if not modules or not isinstance(modules, list):
        return get_default_permissions()
    default_modules = get_default_permissions()
    override_map = _index_by_id(modules)
    merged_modules: List[Dict[str, Any]] = []
    for module in default_modules:
        override = override_map.get(module["id"])
        module_copy = dict(module)
        if override:
            module_copy["roles"] = _merge_roles(module["roles"], override.get("roles"))
            module_copy["actions"] = _merge_actions(
                module["actions"], override.get("actions")
            )
        merged_modules.append(module_copy)
    return merged_modules


def role_has_permission(
    modules: Optional[List[Dict[str, Any]]],
    permission_id: str,
    role: str,
) -> bool:
    data = ensure_permissions(modules)
    for module in data:
        if module["id"] == permission_id:
            return bool(module["roles"].get(role))
        for action in module.get("actions", []):
            if action["id"] == permission_id:
                return bool(action["roles"].get(role))
    return False
