#include "PluginHostBindings.hpp"
#include "PluginHostUiThread.hpp"

#include "slic3r/plugin/PluginAuditManager.hpp"

#include <libslic3r/Config.hpp>
#include <libslic3r/Preset.hpp>
#include <libslic3r/PrintConfig.hpp>

#include <slic3r/GUI/GUI_App.hpp>
#include <slic3r/GUI/Plater.hpp>
#include <slic3r/GUI/Tab.hpp>

#include <boost/log/trivial.hpp>

#include <pybind11/stl.h>

#include <cfloat>
#include <map>
#include <stdexcept>
#include <string>
#include <utility>
#include <vector>

namespace py = pybind11;

// ---------------------------------------------------------------------------
// orca.host.edit -- the only part of the orca.host surface that MUTATES slicer
// settings. Everything else under orca.host is read-only by design.
//
// Three gates stand between a plugin and a preset, and all three must pass:
//   1. the per-plugin settings_write switch, granted by the user in the Plugins
//      dialog (PluginAuditManager::settings_write_granted);
//   2. the option must exist in the target tab's config and its value must
//      survive deserialization and the min/max/enum checks of its ConfigOptionDef;
//   3. no slicing may be in flight.
//
// The write itself reuses Tab::load_config -- the same path the config importer
// and "load from project" take -- so the preset lands in the normal dirty state,
// visible in the UI and revertible with Ctrl+Z.
// ---------------------------------------------------------------------------

namespace Slic3r {
namespace {

Preset::Type preset_type_from_string(const std::string& name)
{
    if (name == "print" || name == "process")
        return Preset::TYPE_PRINT;
    if (name == "filament")
        return Preset::TYPE_FILAMENT;
    if (name == "printer" || name == "machine")
        return Preset::TYPE_PRINTER;
    throw std::invalid_argument("preset_type must be one of 'print', 'filament', 'printer' (got '" + name + "')");
}

// The audit hook never fires for orca.host.edit: these calls cross the C++
// binding directly instead of raising a CPython audit event. So the consent
// check has to happen here, explicitly, on every call.
void require_settings_write()
{
    PluginAuditManager& audit      = PluginAuditManager::instance();
    const std::string   plugin_key = audit.current_plugin();
    if (plugin_key.empty())
        throw std::runtime_error("orca.host.edit must be called from a plugin callback");
    if (!audit.settings_write_granted(plugin_key)) {
        const std::string message =
            "Plugin \"" + plugin_key + "\" is not allowed to modify settings. Enable "
            "\"Allow modifying settings\" for this plugin in File > Plugins.";
        PyErr_SetString(PyExc_PermissionError, message.c_str());
        throw py::error_already_set();
    }
}

const char* option_type_name(ConfigOptionType type)
{
    switch (type) {
    case coFloat:            return "float";
    case coFloats:           return "floats";
    case coInt:              return "int";
    case coInts:             return "ints";
    case coString:           return "string";
    case coStrings:          return "strings";
    case coPercent:          return "percent";
    case coPercents:         return "percents";
    case coFloatOrPercent:   return "float_or_percent";
    case coFloatsOrPercents: return "floats_or_percents";
    case coPoint:            return "point";
    case coPoints:           return "points";
    case coBool:             return "bool";
    case coBools:            return "bools";
    case coEnum:             return "enum";
    default:                 return "other";
    }
}

bool is_numeric_option(ConfigOptionType type)
{
    return type == coFloat || type == coFloats || type == coInt || type == coInts ||
           type == coPercent || type == coPercents || type == coFloatOrPercent ||
           type == coFloatsOrPercents;
}

py::dict option_def_to_dict(const std::string& key, const ConfigOptionDef& def)
{
    py::dict out;
    out["key"]      = key;
    out["label"]    = def.full_label.empty() ? def.label : def.full_label;
    out["category"] = def.category;
    out["tooltip"]  = def.tooltip;
    out["unit"]     = def.sidetext;
    out["type"]     = option_type_name(def.type);
    out["readonly"] = def.readonly;

    // min/max default to +/-FLT_MAX when the definition sets no bound; reporting
    // those as real limits would just invite the model to trust a bogus range.
    if (is_numeric_option(def.type)) {
        if (def.min > -FLT_MAX)
            out["min"] = def.min;
        if (def.max < FLT_MAX)
            out["max"] = def.max;
    }
    if (!def.enum_values.empty()) {
        out["enum_values"] = def.enum_values;
        if (!def.enum_labels.empty())
            out["enum_labels"] = def.enum_labels;
    }
    return out;
}

// orca.host.setting_metadata(keys) -- read-only, and deliberately available even
// without the write grant: knowing an option's bounds is what stops a caller from
// proposing a value that cannot be applied in the first place.
py::dict setting_metadata(const std::vector<std::string>& keys)
{
    py::dict out;
    for (const std::string& key : keys) {
        const ConfigOptionDef* def = print_config_def.get(key);
        if (def == nullptr) {
            py::dict unknown;
            unknown["key"]   = key;
            unknown["error"] = "unknown setting";
            out[py::str(key)] = std::move(unknown);
        } else {
            out[py::str(key)] = option_def_to_dict(key, *def);
        }
    }
    return out;
}

// Plain C++ on purpose: this is filled inside run_on_ui_blocking, which runs with
// the GIL RELEASED. Building py::dict/py::str there would touch the interpreter
// without holding the GIL. The conversion happens in edit_apply, after the helper
// returns and the GIL is back.
struct ApplyOutcome
{
    std::vector<std::string>                         applied;
    std::vector<std::pair<std::string, std::string>> rejected; // key -> reason
};

// Runs on the UI thread. Validates every key first and only then mutates, so a
// single bad value cannot leave the preset half-written.
ApplyOutcome apply_on_ui(const std::map<std::string, std::string>& changes, Preset::Type preset_type)
{
    GUI::GUI_App& app = GUI::wxGetApp();
    if (app.is_closing())
        throw std::runtime_error("OrcaSlicer is shutting down");

    GUI::Plater* plater = app.plater();
    if (plater == nullptr)
        throw std::runtime_error("the plater is not available yet");
    if (plater->is_background_process_slicing())
        throw std::runtime_error("a slicing job is running; apply the settings once it finishes");

    GUI::Tab* tab = app.get_tab(preset_type);
    if (tab == nullptr || tab->get_config() == nullptr)
        throw std::runtime_error("no settings tab for this preset type");

    const DynamicPrintConfig& current = *tab->get_config();
    DynamicPrintConfig        staged  = current;
    ApplyOutcome              outcome;

    for (const auto& [key, value] : changes) {
        if (!current.has(key)) {
            outcome.rejected.emplace_back(key, "not a setting of this preset type");
            continue;
        }
        const ConfigOptionDef* def = print_config_def.get(key);
        if (def != nullptr && def->readonly) {
            outcome.rejected.emplace_back(key, "setting is read-only");
            continue;
        }
        if (def != nullptr && def->type == coEnum && !def->has_enum_value(value)) {
            std::string allowed;
            for (const std::string& v : def->enum_values)
                allowed += (allowed.empty() ? "" : ", ") + v;
            outcome.rejected.emplace_back(key, "not a valid value; expected one of: " + allowed);
            continue;
        }

        try {
            ConfigSubstitutionContext substitutions(ForwardCompatibilitySubstitutionRule::Disable);
            staged.set_deserialize(key, value, substitutions);
        } catch (const std::exception& exc) {
            outcome.rejected.emplace_back(key, std::string("value rejected: ") + exc.what());
            // Roll back this key only: keys validated earlier in the loop stay staged.
            staged.set_key_value(key, current.option(key)->clone());
            continue;
        }

        // Bounds live on the definition, not on the option, so set_deserialize
        // happily accepts an out-of-range number. Check it after the fact and
        // roll the key back if it falls outside.
        if (def != nullptr && is_numeric_option(def->type)) {
            const ConfigOption* opt = staged.option(key);
            double              v   = 0.;
            bool                comparable = false;
            if (opt != nullptr && !opt->is_vector()) {
                try {
                    v          = opt->getFloat();
                    comparable = true;
                } catch (const std::exception&) {
                    comparable = false; // type without a float view; the def has nothing to check
                }
            }
            if (comparable) {
                if (v < def->min || v > def->max) {
                    outcome.rejected.emplace_back(
                        key, "value " + std::to_string(v) + " is outside the allowed range [" +
                                 std::to_string(def->min) + ", " + std::to_string(def->max) + "]");
                    staged.set_key_value(key, current.option(key)->clone());
                    continue;
                }
            }
        }

        outcome.applied.push_back(key);
    }

    if (!outcome.applied.empty()) {
        // Snapshot first: the user must be able to undo an agent's change with Ctrl+Z.
        plater->take_snapshot("Plugin settings change");
        // Same path as the config importer: sets the keys, marks the preset dirty,
        // reloads the UI fields and re-runs the tab's option interlocks.
        tab->load_config(staged);
        plater->schedule_background_process();
        BOOST_LOG_TRIVIAL(info) << "[PLUGIN EDIT] applied " << outcome.applied.size()
                                << " setting(s) to preset type " << int(preset_type);
    }
    return outcome;
}

py::dict edit_apply(const std::map<std::string, std::string>& changes, const std::string& preset_type_name)
{
    require_settings_write();
    if (changes.empty())
        throw std::invalid_argument("changes is empty");

    const Preset::Type preset_type = preset_type_from_string(preset_type_name);
    ApplyOutcome outcome = host_bindings::run_on_ui_blocking(
        [&changes, preset_type]() { return apply_on_ui(changes, preset_type); });

    // GIL is held again here: safe to build the Python objects.
    py::dict rejected;
    for (const auto& [key, reason] : outcome.rejected)
        rejected[py::str(key)] = reason;

    py::dict report;
    report["applied"]       = outcome.applied;
    report["rejected"]      = std::move(rejected);
    report["applied_count"] = outcome.applied.size();
    return report;
}

void edit_reslice()
{
    require_settings_write();
    host_bindings::run_on_ui_blocking([]() {
        GUI::Plater* plater = GUI::wxGetApp().plater();
        if (plater == nullptr)
            throw std::runtime_error("the plater is not available yet");
        plater->schedule_background_process();
    });
}

bool edit_can_write()
{
    PluginAuditManager& audit      = PluginAuditManager::instance();
    const std::string   plugin_key = audit.current_plugin();
    return !plugin_key.empty() && audit.settings_write_granted(plugin_key);
}

} // namespace

void host_bindings::register_edit(py::module_& host)
{
    host.def("setting_metadata", &setting_metadata, py::arg("keys"),
             "Return {key: {label, category, tooltip, unit, type, min, max, enum_values, ...}} "
             "for the given setting keys. Read-only; available without the write permission. "
             "Check an option's bounds here before proposing a value to edit.apply().");

    auto edit = host.def_submodule(
        "edit",
        "Write access to slicer settings. Every call requires the per-plugin "
        "\"Allow modifying settings\" switch in File > Plugins; without it the calls "
        "raise PermissionError. This submodule is absent from stock builds.");

    edit.def("can_write", &edit_can_write,
             "True when the user has granted this plugin the settings-write permission. "
             "Check it to degrade gracefully instead of raising PermissionError.");

    edit.def("apply", &edit_apply, py::arg("changes"), py::arg("preset_type") = "print",
             "Apply {key: serialized_value} to the edited preset of the given type "
             "('print', 'filament' or 'printer'). Values use OrcaSlicer's own serialized "
             "form (\"0.2\", \"15%\", \"1,1,1\"). Keys are validated against their "
             "ConfigOptionDef first, so an invalid one is reported rather than written: "
             "the return value is {applied: [keys], rejected: {key: reason}, applied_count: n}. "
             "Takes an undo snapshot, leaves the preset dirty in the UI, and reschedules "
             "the background process. Refuses while a slicing job is running.");

    edit.def("reslice", &edit_reslice,
             "Reschedule the background slicing process.");
}

} // namespace Slic3r
