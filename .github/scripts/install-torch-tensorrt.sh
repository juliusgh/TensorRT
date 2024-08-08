set -x

TORCH_TORCHVISION=$(grep "^torch" ${PWD}/py/requirements.txt)
INDEX_URL=https://download.pytorch.org/whl/${CHANNEL}/${CU_VERSION}
PLATFORM=$(python -c "import sys; print(sys.platform)")

# Install all the dependencies required for Torch-TensorRT
pip install --pre ${TORCH_TORCHVISION} --index-url ${INDEX_URL}
pip install --pre -r ${PWD}/tests/py/requirements.txt --use-deprecated legacy-resolver

# Install Torch-TensorRT
if [[ ${PLATFORM} == win32 ]]; then
    pip install ${RUNNER_ARTIFACT_DIR}/torch_tensorrt*.whl
else
    pip install /opt/torch-tensorrt-builds/torch_tensorrt*.whl
fi

pip list | grep torch

ls -lart $CONDA_ENV/lib/site-packages | grep torch_tensorrt

ls -lart $CONDA_ENV/lib/site-packages/torch_tensorrt/lib

pip show torch_tensorrt | grep Location:

echo LD_LIBRARY_PATH=${LD_LIBRARY_PATH}

echo -e "Running test script";
