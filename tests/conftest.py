from tv_avatar.catalog import AvatarProfile, LanguageProfile, SessionPersona

#: A persona that never touches avatars.yaml, for tests of the session and
#: pipeline layers that do not care which avatar is speaking.
PERSONA = SessionPersona(
    avatar=AvatarProfile(id="test", name="Test", anam_avatar_id="avatar-1", voice="voice-1"),
    language=LanguageProfile(code="en", name="English", native_name="English"),
)
