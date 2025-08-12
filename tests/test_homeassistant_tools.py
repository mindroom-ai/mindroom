"""Tests for the custom Home Assistant tools."""

import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, Mock, patch

import pytest
from httpx import Response

from mindroom.credentials import CredentialsManager
from mindroom.custom_tools.homeassistant import HomeAssistantTools

from .conftest import TEST_PASSWORD


@pytest.fixture
def mock_credentials_manager(tmp_path: Path) -> CredentialsManager:
    """Create a mock credentials manager with test data."""
    manager = CredentialsManager(base_path=tmp_path / "test_creds")

    # Save test Home Assistant credentials
    test_creds = {
        "instance_url": "http://homeassistant.local:8123",
        "access_token": TEST_PASSWORD,
    }
    manager.save_credentials("homeassistant", test_creds)
    return manager


@pytest.fixture
def ha_tools_with_mocked_creds(mock_credentials_manager: CredentialsManager) -> HomeAssistantTools:
    """Create HomeAssistantTools instance with mocked credentials."""
    with patch("mindroom.custom_tools.homeassistant.get_credentials_manager") as mock_get_manager:
        mock_get_manager.return_value = mock_credentials_manager
        return HomeAssistantTools()


class TestHomeAssistantTools:
    """Test suite for Home Assistant tools."""

    def test_initialization(self) -> None:
        """Test HomeAssistantTools initialization."""
        with patch("mindroom.custom_tools.homeassistant.get_credentials_manager") as mock_get_manager:
            mock_manager = MagicMock()
            mock_get_manager.return_value = mock_manager

            ha_tools = HomeAssistantTools()

            # Verify credentials manager was obtained
            mock_get_manager.assert_called_once()

            # Verify tools were registered
            assert ha_tools.name == "homeassistant"
            assert len(ha_tools.tools) == 11

            # Verify all expected methods are registered
            expected_methods = [
                "get_entity_state",
                "list_entities",
                "turn_on",
                "turn_off",
                "toggle",
                "set_brightness",
                "set_color",
                "set_temperature",
                "activate_scene",
                "trigger_automation",
                "call_service",
            ]
            method_names = [tool.__name__ for tool in ha_tools.tools]
            for expected in expected_methods:
                assert expected in method_names

    def test_load_config_with_stored_credentials(
        self,
        ha_tools_with_mocked_creds: HomeAssistantTools,
    ) -> None:
        """Test loading configuration from stored credentials."""
        config = ha_tools_with_mocked_creds._load_config()

        assert config is not None
        assert config["instance_url"] == "http://homeassistant.local:8123"
        assert config["access_token"] == TEST_PASSWORD

        # Should return cached config on second call
        config2 = ha_tools_with_mocked_creds._load_config()
        assert config2 is config

    def test_load_config_without_credentials(self) -> None:
        """Test loading configuration when no credentials exist."""
        with patch("mindroom.custom_tools.homeassistant.get_credentials_manager") as mock_get_manager:
            mock_manager = MagicMock()
            mock_manager.load_credentials.return_value = None
            mock_get_manager.return_value = mock_manager

            ha_tools = HomeAssistantTools()
            config = ha_tools._load_config()

            assert config is None

    @pytest.mark.asyncio
    async def test_api_request_without_config(self) -> None:
        """Test API request when no configuration exists."""
        with patch("mindroom.custom_tools.homeassistant.get_credentials_manager") as mock_get_manager:
            mock_manager = MagicMock()
            mock_manager.load_credentials.return_value = None
            mock_get_manager.return_value = mock_manager

            ha_tools = HomeAssistantTools()
            result = await ha_tools._api_request("GET", "/api/states")

            assert result == {"error": "Home Assistant is not configured. Please connect through the widget."}

    @pytest.mark.asyncio
    async def test_api_request_missing_credentials(self) -> None:
        """Test API request with incomplete credentials."""
        with patch("mindroom.custom_tools.homeassistant.get_credentials_manager") as mock_get_manager:
            mock_manager = MagicMock()
            # Missing access_token
            mock_manager.load_credentials.return_value = {"instance_url": "http://localhost"}
            mock_get_manager.return_value = mock_manager

            ha_tools = HomeAssistantTools()
            result = await ha_tools._api_request("GET", "/api/states")

            assert result == {"error": "Missing Home Assistant credentials"}

    @pytest.mark.asyncio
    async def test_api_request_success(self, ha_tools_with_mocked_creds: HomeAssistantTools) -> None:
        """Test successful API request."""
        with patch("httpx.AsyncClient") as mock_client_class:
            mock_client = AsyncMock()
            mock_response = Mock(spec=Response)
            mock_response.status_code = 200
            mock_response.json.return_value = {"success": True}
            mock_client.request.return_value = mock_response
            mock_client_class.return_value.__aenter__.return_value = mock_client

            result = await ha_tools_with_mocked_creds._api_request("GET", "/api/states")

            assert result == {"success": True}
            mock_client.request.assert_called_once_with(
                method="GET",
                url="http://homeassistant.local:8123/api/states",
                headers={"Authorization": f"Bearer {TEST_PASSWORD}"},
                json=None,
                timeout=10.0,
            )

    @pytest.mark.asyncio
    async def test_api_request_with_json_data(
        self,
        ha_tools_with_mocked_creds: HomeAssistantTools,
    ) -> None:
        """Test API request with JSON data."""
        with patch("httpx.AsyncClient") as mock_client_class:
            mock_client = AsyncMock()
            mock_response = Mock(spec=Response)
            mock_response.status_code = 200
            mock_response.json.return_value = {"success": True}
            mock_client.request.return_value = mock_response
            mock_client_class.return_value.__aenter__.return_value = mock_client

            json_data = {"entity_id": "light.living_room"}
            result = await ha_tools_with_mocked_creds._api_request(
                "POST",
                "/api/services/light/turn_on",
                json_data,
            )

            assert result == {"success": True}
            mock_client.request.assert_called_once_with(
                method="POST",
                url="http://homeassistant.local:8123/api/services/light/turn_on",
                headers={"Authorization": f"Bearer {TEST_PASSWORD}"},
                json=json_data,
                timeout=10.0,
            )

    @pytest.mark.asyncio
    async def test_api_request_error_response(
        self,
        ha_tools_with_mocked_creds: HomeAssistantTools,
    ) -> None:
        """Test API request with error response."""
        with patch("httpx.AsyncClient") as mock_client_class:
            mock_client = AsyncMock()
            mock_response = Mock(spec=Response)
            mock_response.status_code = 404
            mock_response.text = "Not found"
            mock_client.request.return_value = mock_response
            mock_client_class.return_value.__aenter__.return_value = mock_client

            result = await ha_tools_with_mocked_creds._api_request("GET", "/api/invalid")

            assert result == {"error": "API error: Not found"}

    @pytest.mark.asyncio
    async def test_api_request_network_error(
        self,
        ha_tools_with_mocked_creds: HomeAssistantTools,
    ) -> None:
        """Test API request with network error."""
        with patch("httpx.AsyncClient") as mock_client_class:
            mock_client = AsyncMock()
            mock_client.request.side_effect = Exception("Network error")
            mock_client_class.return_value.__aenter__.return_value = mock_client

            result = await ha_tools_with_mocked_creds._api_request("GET", "/api/states")

            assert result == {"error": "Unexpected error: Network error"}

    @pytest.mark.asyncio
    async def test_get_entity_state(self, ha_tools_with_mocked_creds: HomeAssistantTools) -> None:
        """Test getting entity state."""
        with patch.object(ha_tools_with_mocked_creds, "_api_request") as mock_request:
            mock_request.return_value = {
                "entity_id": "light.living_room",
                "state": "on",
                "attributes": {"brightness": 255},
            }

            result = await ha_tools_with_mocked_creds.get_entity_state("light.living_room")
            result_dict = json.loads(result)

            assert result_dict["entity_id"] == "light.living_room"
            assert result_dict["state"] == "on"
            assert result_dict["attributes"]["brightness"] == 255

            mock_request.assert_called_once_with("GET", "/api/states/light.living_room")

    @pytest.mark.asyncio
    async def test_list_entities(self, ha_tools_with_mocked_creds: HomeAssistantTools) -> None:
        """Test listing entities."""
        with patch.object(ha_tools_with_mocked_creds, "_api_request") as mock_request:
            mock_request.return_value = [
                {
                    "entity_id": "light.living_room",
                    "state": "on",
                    "attributes": {"friendly_name": "Living Room Light"},
                },
                {
                    "entity_id": "switch.bedroom",
                    "state": "off",
                    "attributes": {"friendly_name": "Bedroom Switch"},
                },
            ]

            result = await ha_tools_with_mocked_creds.list_entities()
            result_list = json.loads(result)

            assert len(result_list) == 2
            assert result_list[0]["entity_id"] == "light.living_room"
            assert result_list[0]["friendly_name"] == "Living Room Light"
            assert result_list[1]["entity_id"] == "switch.bedroom"
            assert result_list[1]["friendly_name"] == "Bedroom Switch"

    @pytest.mark.asyncio
    async def test_turn_on(self, ha_tools_with_mocked_creds: HomeAssistantTools) -> None:
        """Test turning on an entity."""
        with patch.object(ha_tools_with_mocked_creds, "_api_request") as mock_request:
            mock_request.return_value = {"success": True}

            result = await ha_tools_with_mocked_creds.turn_on("light.living_room")
            result_dict = json.loads(result)

            assert result_dict["success"] is True
            mock_request.assert_called_once_with(
                "POST",
                "/api/services/light/turn_on",
                {"entity_id": "light.living_room"},
            )

    @pytest.mark.asyncio
    async def test_set_brightness_valid(
        self,
        ha_tools_with_mocked_creds: HomeAssistantTools,
    ) -> None:
        """Test setting brightness with valid value."""
        with patch.object(ha_tools_with_mocked_creds, "_api_request") as mock_request:
            mock_request.return_value = {"success": True}

            result = await ha_tools_with_mocked_creds.set_brightness("light.living_room", 128)
            result_dict = json.loads(result)

            assert result_dict["success"] is True
            mock_request.assert_called_once_with(
                "POST",
                "/api/services/light/turn_on",
                {"entity_id": "light.living_room", "brightness": 128},
            )

    @pytest.mark.asyncio
    async def test_set_brightness_invalid(
        self,
        ha_tools_with_mocked_creds: HomeAssistantTools,
    ) -> None:
        """Test setting brightness with invalid value."""
        result = await ha_tools_with_mocked_creds.set_brightness("light.living_room", 300)
        result_dict = json.loads(result)

        assert result_dict["error"] == "Brightness must be between 0 and 255"

    @pytest.mark.asyncio
    async def test_set_color_valid(self, ha_tools_with_mocked_creds: HomeAssistantTools) -> None:
        """Test setting color with valid RGB values."""
        with patch.object(ha_tools_with_mocked_creds, "_api_request") as mock_request:
            mock_request.return_value = {"success": True}

            result = await ha_tools_with_mocked_creds.set_color("light.living_room", 255, 128, 0)
            result_dict = json.loads(result)

            assert result_dict["success"] is True
            mock_request.assert_called_once_with(
                "POST",
                "/api/services/light/turn_on",
                {"entity_id": "light.living_room", "rgb_color": [255, 128, 0]},
            )

    @pytest.mark.asyncio
    async def test_set_color_invalid(self, ha_tools_with_mocked_creds: HomeAssistantTools) -> None:
        """Test setting color with invalid RGB values."""
        result = await ha_tools_with_mocked_creds.set_color("light.living_room", 255, 300, 0)
        result_dict = json.loads(result)

        assert result_dict["error"] == "RGB values must be between 0 and 255"

    @pytest.mark.asyncio
    async def test_call_service_with_json_data(
        self,
        ha_tools_with_mocked_creds: HomeAssistantTools,
    ) -> None:
        """Test calling a service with JSON data string."""
        with patch.object(ha_tools_with_mocked_creds, "_api_request") as mock_request:
            mock_request.return_value = {"success": True}

            data_json = '{"brightness": 200, "transition": 2}'
            result = await ha_tools_with_mocked_creds.call_service(
                "light",
                "turn_on",
                "light.living_room",
                data_json,
            )
            result_dict = json.loads(result)

            assert result_dict["success"] is True
            mock_request.assert_called_once_with(
                "POST",
                "/api/services/light/turn_on",
                {"entity_id": "light.living_room", "brightness": 200, "transition": 2},
            )

    @pytest.mark.asyncio
    async def test_call_service_invalid_json(
        self,
        ha_tools_with_mocked_creds: HomeAssistantTools,
    ) -> None:
        """Test calling a service with invalid JSON data."""
        result = await ha_tools_with_mocked_creds.call_service(
            "light",
            "turn_on",
            "light.living_room",
            "invalid json{",
        )
        result_dict = json.loads(result)

        assert "Invalid JSON in data parameter" in result_dict["error"]
