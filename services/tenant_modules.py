from __future__ import annotations

from typing import Iterable, List, TypedDict


class TenantModuleDef(TypedDict):
    id: str
    label: str
    description: str
    required: bool
    platform_visible: bool
    enabled_by_default: bool


TENANT_MODULES: List[TenantModuleDef] = [
    {
        "id": "dashboard",
        "label": "Inicio",
        "description": "Panel principal con indicadores generales.",
        "required": True,
        "platform_visible": True,
        "enabled_by_default": True,
    },
    {
        "id": "products",
        "label": "Productos",
        "description": "Catalogo e inventario base del negocio.",
        "required": True,
        "platform_visible": True,
        "enabled_by_default": True,
    },
    {
        "id": "movements",
        "label": "Movimientos",
        "description": "Entradas, salidas y control de stock.",
        "required": True,
        "platform_visible": True,
        "enabled_by_default": True,
    },
    {
        "id": "pos",
        "label": "POS / Caja",
        "description": "Punto de venta y operaciones de caja.",
        "required": True,
        "platform_visible": True,
        "enabled_by_default": True,
    },
    {
        "id": "documents",
        "label": "Documentos",
        "description": "Separados y documentos relacionados.",
        "required": True,
        "platform_visible": True,
        "enabled_by_default": True,
    },
    {
        "id": "reports",
        "label": "Reportes",
        "description": "Informes y analitica del negocio.",
        "required": True,
        "platform_visible": True,
        "enabled_by_default": True,
    },
    {
        "id": "settings",
        "label": "Configuracion",
        "description": "Preferencias generales y ajustes del software.",
        "required": True,
        "platform_visible": True,
        "enabled_by_default": True,
    },
    {
        "id": "labels",
        "label": "Etiquetas",
        "description": "Generacion e impresion de etiquetas.",
        "required": False,
        "platform_visible": True,
        "enabled_by_default": True,
    },
    {
        "id": "labels_pilot",
        "label": "Etiquetado (beta)",
        "description": "Flujo beta de etiquetado avanzado.",
        "required": False,
        "platform_visible": True,
        "enabled_by_default": True,
    },
    {
        "id": "hr",
        "label": "Recursos Humanos",
        "description": "Gestion de empleados y datos laborales.",
        "required": False,
        "platform_visible": True,
        "enabled_by_default": True,
    },
    {
        "id": "investment",
        "label": "Inversion",
        "description": "Seguimiento privado de inversion familiar.",
        "required": False,
        "platform_visible": True,
        "enabled_by_default": False,
    },
    {
        "id": "commerce_web",
        "label": "Comercio Web",
        "description": "Ordenes web, pagos online y conversion a venta.",
        "required": False,
        "platform_visible": True,
        "enabled_by_default": False,
    },
    {
        "id": "sales_history",
        "label": "Historial de ventas",
        "description": "Lectura historica de ventas y seguimiento.",
        "required": True,
        "platform_visible": False,
        "enabled_by_default": True,
    },
    {
        "id": "users",
        "label": "Usuarios",
        "description": "Gestion interna de usuarios POS.",
        "required": True,
        "platform_visible": False,
        "enabled_by_default": True,
    },
    {
        "id": "schedule",
        "label": "Agenda",
        "description": "Horarios y turnos del personal.",
        "required": False,
        "platform_visible": False,
        "enabled_by_default": False,
    },
]


def get_tenant_module_catalog() -> List[TenantModuleDef]:
    return [dict(item) for item in TENANT_MODULES]


MODULE_IDS = {item["id"] for item in TENANT_MODULES}
REQUIRED_MODULE_IDS = {item["id"] for item in TENANT_MODULES if item["required"]}
DEFAULT_ENABLED_MODULE_IDS = {
    item["id"] for item in TENANT_MODULES if item["enabled_by_default"] or item["required"]
}


def normalize_enabled_modules(modules: Iterable[str] | None) -> List[str]:
    if modules is None:
        normalized = set(DEFAULT_ENABLED_MODULE_IDS)
    else:
        normalized = {
            module_id.strip()
            for module_id in modules
            if isinstance(module_id, str) and module_id.strip() in MODULE_IDS
        }
    normalized.update(REQUIRED_MODULE_IDS)
    return [item["id"] for item in TENANT_MODULES if item["id"] in normalized]


def is_module_enabled(enabled_modules: Iterable[str] | None, module_id: str) -> bool:
    return module_id in set(normalize_enabled_modules(enabled_modules))
