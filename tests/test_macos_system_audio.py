from macos_system_audio import get_permission_guidance

def test_get_permission_guidance():
    """Test that get_permission_guidance returns the expected guidance string."""
    guidance = get_permission_guidance()

    assert isinstance(guidance, str)
    assert len(guidance) > 0

    expected_phrases = [
        "Per registrare l'audio di sistema",
        "macOS richiede il permesso 'Registrazione schermo'",
        "1. Apri Impostazioni di Sistema → Privacy e Sicurezza → Registrazione schermo",
        "2. Abilita l'applicazione",
        "3. Riavvia la registrazione",
        "Nessun driver né configurazione audio aggiuntiva è necessaria."
    ]

    for phrase in expected_phrases:
        assert phrase in guidance
