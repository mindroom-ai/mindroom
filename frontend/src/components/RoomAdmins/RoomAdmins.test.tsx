import { beforeEach, describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { RoomAdmins } from "./RoomAdmins";
import { isConcreteMatrixUserId } from "@/lib/matrixIds";
import { useConfigStore } from "@/store/configStore";
import type { ConfigDiagnostic } from "@/lib/configValidation";
import { Config } from "@/types/config";
import type { SaveConfigResult } from "@/store/configStore";

vi.mock("@/store/configStore");

const { mockToast, mockToaster } = vi.hoisted(() => ({
  mockToast: vi.fn(),
  mockToaster: vi.fn(),
}));
vi.mock("@/components/ui/use-toast", () => ({
  useToast: () => ({ toast: mockToast }),
}));
vi.mock("@/components/ui/toaster", () => ({
  toast: mockToaster,
}));

describe("isConcreteMatrixUserId", () => {
  it("accepts full Matrix user IDs and rejects wildcards or placeholders", () => {
    expect(isConcreteMatrixUserId("@alice:example.com")).toBe(true);
    expect(isConcreteMatrixUserId("alice:example.com")).toBe(false);
    expect(isConcreteMatrixUserId("@alice")).toBe(false);
    expect(isConcreteMatrixUserId("@*:example.com")).toBe(false);
    expect(isConcreteMatrixUserId("__OWNER_PLACEHOLDER__")).toBe(false);
  });
});

describe("RoomAdmins", () => {
  const mockSaveConfig = vi.fn();
  const mockUpdateMatrixRoomAccess = vi.fn();
  type MockStoreState = {
    config: Config;
    diagnostics: ConfigDiagnostic[];
    syncStatus: "synced" | "syncing" | "error" | "disconnected";
    isDirty: boolean;
    isLoading: boolean;
    saveConfig: () => Promise<SaveConfigResult>;
    updateMatrixRoomAccess: typeof mockUpdateMatrixRoomAccess;
  };
  type MockedStoreHook = {
    (): MockStoreState;
    getState: () => MockStoreState;
    mockReturnValue: (value: MockStoreState) => void;
  };
  const mockedUseConfigStore = useConfigStore as unknown as MockedStoreHook;
  let mockStoreState: MockStoreState;

  const createConfig = (): Partial<Config> => ({
    matrix_room_access: {
      mode: "multi_user",
      room_admins: ["@alice:example.com"],
    },
  });

  const setMockStore = (config: Partial<Config>) => {
    mockStoreState = {
      config: config as Config,
      diagnostics: [],
      syncStatus: "synced",
      isDirty: false,
      isLoading: false,
      saveConfig: mockSaveConfig,
      updateMatrixRoomAccess: mockUpdateMatrixRoomAccess,
    };
    mockedUseConfigStore.mockReturnValue(mockStoreState);
    mockedUseConfigStore.getState = vi.fn(() => mockStoreState);
  };

  beforeEach(() => {
    vi.clearAllMocks();
    mockSaveConfig.mockImplementation(async () => {
      mockStoreState.syncStatus = "synced";
      mockStoreState.isDirty = false;
      return { status: "saved" };
    });
    setMockStore(createConfig());
  });

  it("lists configured room admins", () => {
    render(<RoomAdmins />);

    expect(screen.getByText("Room Admins")).toBeInTheDocument();
    expect(screen.getByText("@alice:example.com")).toBeInTheDocument();
  });

  it("adds a new admin and preserves other access settings", async () => {
    render(<RoomAdmins />);

    fireEvent.change(screen.getByPlaceholderText("@alice:example.com"), {
      target: { value: "@bob:example.com" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Add" }));

    await waitFor(() => {
      expect(mockUpdateMatrixRoomAccess).toHaveBeenCalledWith({
        mode: "multi_user",
        room_admins: ["@alice:example.com", "@bob:example.com"],
      });
    });
  });

  it("disables Add and Save while the config has not loaded", () => {
    setMockStore(null as unknown as Partial<Config>);

    render(<RoomAdmins />);

    fireEvent.change(screen.getByPlaceholderText("@alice:example.com"), {
      target: { value: "@bob:example.com" },
    });

    expect(screen.getByRole("button", { name: "Add" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "Save" })).toBeDisabled();
    expect(mockUpdateMatrixRoomAccess).not.toHaveBeenCalled();
  });

  it("adds an admin when the config has no matrix_room_access section", async () => {
    setMockStore({});

    render(<RoomAdmins />);

    fireEvent.change(screen.getByPlaceholderText("@alice:example.com"), {
      target: { value: "@bob:example.com" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Add" }));

    await waitFor(() => {
      expect(mockUpdateMatrixRoomAccess).toHaveBeenCalledWith({
        room_admins: ["@bob:example.com"],
      });
    });
  });

  it("rejects invalid Matrix user IDs", async () => {
    render(<RoomAdmins />);

    fireEvent.change(screen.getByPlaceholderText("@alice:example.com"), {
      target: { value: "not-a-user-id" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Add" }));

    await waitFor(() => {
      expect(mockToast).toHaveBeenCalledWith(
        expect.objectContaining({ title: "Invalid Matrix user ID" }),
      );
    });
    expect(mockUpdateMatrixRoomAccess).not.toHaveBeenCalled();
  });

  it("rejects duplicate admins", async () => {
    render(<RoomAdmins />);

    fireEvent.change(screen.getByPlaceholderText("@alice:example.com"), {
      target: { value: "@alice:example.com" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Add" }));

    await waitFor(() => {
      expect(mockToast).toHaveBeenCalledWith(
        expect.objectContaining({ title: "Already a room admin" }),
      );
    });
    expect(mockUpdateMatrixRoomAccess).not.toHaveBeenCalled();
  });

  it("removes an admin", async () => {
    render(<RoomAdmins />);

    fireEvent.click(
      screen.getByRole("button", { name: "Remove @alice:example.com" }),
    );

    await waitFor(() => {
      expect(mockUpdateMatrixRoomAccess).toHaveBeenCalledWith({
        mode: "multi_user",
        room_admins: [],
      });
    });
  });

  it("saves and shows a confirmation toast", async () => {
    render(<RoomAdmins />);

    fireEvent.click(screen.getByRole("button", { name: "Save" }));

    await waitFor(() => {
      expect(mockSaveConfig).toHaveBeenCalled();
      expect(mockToast).toHaveBeenCalledWith(
        expect.objectContaining({ title: "Room Admins Saved" }),
      );
    });
  });

  it("shows an error toast when saving fails", async () => {
    mockSaveConfig.mockImplementation(async () => {
      mockStoreState.syncStatus = "error";
      mockStoreState.isDirty = true;
      return {
        status: "error",
        message: "Configuration validation failed",
        diagnostics: mockStoreState.diagnostics,
      };
    });

    render(<RoomAdmins />);

    fireEvent.click(screen.getByRole("button", { name: "Save" }));

    await waitFor(() => {
      expect(mockToaster).toHaveBeenCalledWith({
        title: "Save Failed",
        description: "Configuration validation failed",
        variant: "destructive",
      });
    });
    expect(mockToast).not.toHaveBeenCalledWith(
      expect.objectContaining({ title: "Room Admins Saved" }),
    );
  });
});
