from services.permissions import (
    LOCKED_ACTION_IDS,
    get_default_permissions,
    role_has_permission,
)
from services import permissions


def test_pos_operational_read_permissions_for_seller_and_supervisor():
    """
    Invariante operativa:
    Si un rol opera POS, debe poder leer configuracion y metodos de pago
    para evitar bloqueos al vender/imprimir.
    """
    modules = get_default_permissions()

    for role in ("Vendedor", "Supervisor"):
        assert role_has_permission(modules, "pos.sales", role) is True
        assert role_has_permission(modules, "pos.returns", role) is True
        assert role_has_permission(modules, "settings.view", role) is True
        assert (
            role_has_permission(modules, "settings.payment_methods.view", role)
            is True
        )


def test_seller_cannot_manage_settings_or_payment_methods():
    """
    Seguridad:
    Vendedor puede operar (read), pero no administrar configuraciones.
    """
    modules = get_default_permissions()

    assert role_has_permission(modules, "settings.manage", "Vendedor") is False
    assert (
        role_has_permission(modules, "settings.payment_methods", "Vendedor")
        is False
    )


def test_only_locked_actions_are_non_revocable_floor():
    defaults = get_default_permissions()

    # Override hostil: intenta apagar módulos y acciones.
    override = [
        {
            "id": "dashboard",
            "roles": {
                "Administrador": False,
                "Supervisor": False,
                "Vendedor": False,
                "Auditor": False,
            },
            "actions": [
                {
                    "id": "dashboard.view",
                    "roles": {
                        "Administrador": False,
                        "Supervisor": False,
                        "Vendedor": False,
                        "Auditor": False,
                    },
                },
            ],
        },
        {
            "id": "pos",
            "roles": {"Administrador": False, "Supervisor": False, "Vendedor": False},
            "actions": [
                {
                    "id": "pos.sales",
                    "roles": {"Administrador": False, "Supervisor": False, "Vendedor": False},
                },
            ],
        },
        {
            "id": "settings",
            "roles": {"Administrador": False},
            "actions": [
                {
                    "id": "settings.view",
                    "roles": {
                        "Administrador": False,
                        "Supervisor": False,
                        "Vendedor": False,
                        "Auditor": False,
                    },
                },
                {
                    "id": "settings.payment_methods.view",
                    "roles": {
                        "Administrador": False,
                        "Supervisor": False,
                        "Vendedor": False,
                    },
                },
            ],
        },
    ]

    merged = permissions.ensure_permissions(override)

    # Acciones bloqueadas: todo True en defaults debe permanecer True.
    for permission_id in LOCKED_ACTION_IDS:
        for role in ("Administrador", "Supervisor", "Vendedor", "Auditor"):
            default_value = role_has_permission(defaults, permission_id, role)
            if default_value:
                assert role_has_permission(merged, permission_id, role) is True

    # Acciones no bloqueadas: deben poder cambiarse.
    assert role_has_permission(merged, "dashboard.view", "Vendedor") is False
    assert role_has_permission(merged, "pos.sales", "Vendedor") is False
