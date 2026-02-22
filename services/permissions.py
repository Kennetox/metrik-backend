from __future__ import annotations

from copy import deepcopy
from typing import Any, Dict, List, Optional


ROLE_KEYS = ["Administrador", "Supervisor", "Vendedor", "Auditor"]

# Acciones críticas mínimas que deben quedar protegidas para operación.
# El resto de acciones/módulos siguen siendo editables por toggles.
LOCKED_ACTION_IDS = {
    "settings.view",
    "settings.payment_methods.view",
}


def _role_flags(**overrides: bool) -> Dict[str, bool]:
    flags = {role: False for role in ROLE_KEYS}
    for role, value in overrides.items():
        if role in flags:
            flags[role] = bool(value)
    return flags


def _apply_locked_editable_flags(modules: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    for module in modules:
        actions = module.get("actions", [])
        for action in actions:
            action_id = action.get("id")
            if action_id in LOCKED_ACTION_IDS:
                action["editable"] = False
    return modules


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
            },
            {
                "id": "dashboard.today",
                "label": "Ver métricas de hoy",
                "description": "Muestra indicadores operativos del día actual.",
                "roles": _role_flags(
                    Administrador=True, Supervisor=True, Vendedor=True, Auditor=True
                ),
            },
            {
                "id": "dashboard.history",
                "label": "Ver histórico (semana/mes)",
                "description": "Muestra KPIs y gráficas históricas del dashboard.",
                "roles": _role_flags(
                    Administrador=True, Supervisor=True, Auditor=True
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
                "id": "pos.returns.void",
                "label": "Anular devoluciones",
                "description": "Permite anular devoluciones registradas.",
                "roles": _role_flags(Administrador=True, Supervisor=True),
            },
            {
                "id": "pos.changes.void",
                "label": "Anular cambios",
                "description": "Permite anular cambios registrados.",
                "roles": _role_flags(Administrador=True, Supervisor=True),
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
            },
            {
                "id": "documents.sales.adjust",
                "label": "Ajustar ventas",
                "description": "Permite registrar ajustes sobre ventas.",
                "roles": _role_flags(Administrador=True, Supervisor=True),
            },
            {
                "id": "documents.sales.void",
                "label": "Anular ventas",
                "description": "Permite anular ventas (si aplica).",
                "roles": _role_flags(Administrador=True, Supervisor=True),
            },
            {
                "id": "documents.separated_orders.void_payment",
                "label": "Anular abonos",
                "description": "Permite anular pagos de separados.",
                "roles": _role_flags(Administrador=True, Supervisor=True),
            },
        ],
    },
    {
        "id": "sales_history",
        "label": "Historial de ventas",
        "description": "Consulta de ventas, reimpresión y seguimiento.",
        "roles": _role_flags(
            Administrador=True, Supervisor=True, Vendedor=True, Auditor=True
        ),
        "actions": [
            {
                "id": "sales_history.view",
                "label": "Ver historial de ventas",
                "roles": _role_flags(
                    Administrador=True, Supervisor=True, Vendedor=True, Auditor=True
                ),
            },
            {
                "id": "sales_history.history",
                "label": "Ver histórico por rango",
                "description": "Permite cambiar rangos de fecha en historial de ventas.",
                "roles": _role_flags(
                    Administrador=True, Supervisor=True, Auditor=True
                ),
            },
        ],
    },
    {
        "id": "products",
        "label": "Productos",
        "description": "Catálogo e inventario.",
        "roles": _role_flags(Administrador=True, Supervisor=True),
        "actions": [
            {
                "id": "products.view",
                "label": "Ver productos",
                "description": "Permite consultar catálogo y grupos para el POS.",
                "roles": _role_flags(
                    Administrador=True, Supervisor=True, Vendedor=True, Auditor=True
                ),
            },
            {
                "id": "products.manage",
                "label": "Administrar productos",
                "roles": _role_flags(Administrador=True, Supervisor=True),
            },
            {
                "id": "products.import",
                "label": "Importar productos",
                "description": "Permite importar productos masivamente desde Excel.",
                "roles": _role_flags(Administrador=True),
            },
        ],
    },
    {
        "id": "movements",
        "label": "Movimientos",
        "description": "Movimientos y control de stock.",
        "roles": _role_flags(Administrador=True, Supervisor=True),
        "actions": [
            {
                "id": "movements.view",
                "label": "Ver movimientos",
                "description": "Consultar métricas, historial y estado del stock.",
                "roles": _role_flags(Administrador=True, Supervisor=True),
            },
            {
                "id": "movements.manage",
                "label": "Registrar movimientos",
                "description": "Crear ajustes y movimientos manuales de inventario.",
                "roles": _role_flags(Administrador=True, Supervisor=True),
            },
        ],
    },
    {
        "id": "labels",
        "label": "Etiquetas",
        "description": "Generación de archivos para etiquetas.",
        "roles": _role_flags(Administrador=True, Supervisor=True, Vendedor=True),
        "actions": [
            {
                "id": "labels.export",
                "label": "Exportar etiquetas",
                "roles": _role_flags(Administrador=True, Supervisor=True, Vendedor=True),
            },
        ],
    },
    {
        "id": "labels_pilot",
        "label": "Etiquetado (beta)",
        "description": "Vista beta para flujo de etiquetado.",
        "roles": _role_flags(Administrador=True, Supervisor=True, Vendedor=True),
        "actions": [
            {
                "id": "labels.pilot.view",
                "label": "Ver etiquetado beta",
                "description": "Permite acceder a la vista Etiquetado (beta).",
                "roles": _role_flags(Administrador=True, Supervisor=True, Vendedor=True),
            }
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
                "id": "settings.view",
                "label": "Ver configuración POS",
                "description": "Permite consultar las preferencias para el POS.",
                "roles": _role_flags(
                    Administrador=True, Supervisor=True, Vendedor=True
                ),
            },
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
            {
                "id": "settings.payment_methods.view",
                "label": "Ver métodos de pago",
                "description": "Permite consultar los métodos de pago desde el POS.",
                "roles": _role_flags(
                    Administrador=True, Supervisor=True, Vendedor=True
                ),
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
    data = deepcopy(DEFAULT_ROLE_PERMISSION_MODULES)
    return _apply_locked_editable_flags(data)


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


def _enforce_locked_action_floor(
    merged_modules: List[Dict[str, Any]],
    default_modules: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    default_action_roles: Dict[str, Dict[str, bool]] = {}
    for module in default_modules:
        for action in module.get("actions", []):
            action_id = action.get("id")
            if not action_id:
                continue
            default_action_roles[action_id] = dict(action.get("roles", {}))

    for module in merged_modules:
        for action in module.get("actions", []):
            action_id = action.get("id")
            if action_id not in LOCKED_ACTION_IDS:
                continue
            baseline_roles = default_action_roles.get(action_id, {})
            current_roles = dict(action.get("roles", {}))
            for role in ROLE_KEYS:
                if baseline_roles.get(role) is True:
                    current_roles[role] = True
            action["roles"] = current_roles

    return merged_modules


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
    merged_modules = _enforce_locked_action_floor(merged_modules, default_modules)
    return _apply_locked_editable_flags(merged_modules)


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
