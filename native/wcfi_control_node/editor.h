#pragma once
#include "pluginterfaces/gui/iplugview.h"
#include "pluginterfaces/vst/vsttypes.h"

namespace wcfi_control {
class Controller;
enum class EditResult { Applied, InvalidValue, Conflict, HostUnavailable };

// Shared by the native controls and their fake-host tests. One explicit gesture,
// original-value conflict detection, and no retry/rollback on a host failure.
EditResult commit_control_edit(Controller&, Steinberg::Vst::ParamID,
                              double expected_normalized, double plain_value);
Steinberg::IPlugView* create_control_editor(Controller*);
}
