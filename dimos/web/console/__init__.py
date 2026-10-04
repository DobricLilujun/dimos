# Copyright 2026 Dimensional Inc.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""SEDAN GROUP robot console: a web control deck + ChatGPT-style chat.

See :mod:`dimos.web.console.module` for the module and
:mod:`dimos.web.console.frontend` for the served single-page UI.
"""

from dimos.web.console.module import RobotConsoleModule, RobotConsoleModuleConfig

__all__ = ["RobotConsoleModule", "RobotConsoleModuleConfig"]